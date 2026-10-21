import os
import time
#socket：Python 内置的网络通信标准库，提供底层网络接口。这里用它做 端口探活
import socket

from utils.logger_handler import logger

_redis = None
_ENABLED = False
_CHECKED = False
# 上次连接失败的时间点（monotonic）。失败后不永久放弃，隔一会儿再试一次：
# Redis 重启 / 网络抖动恢复后，服务应该自己接上，而不是必须重启应用进程。
# 这是「一次性初始化」最典型的坑——本地开发时根本发现不了（你总是连得上）。
_RETRY_INTERVAL = 30.0
_last_failure_at = 0.0


def _check_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        #向操作系统内核申请一个 文件描述符（fd），创建一个 IPv4 TCP 套接字。AF_INET 指定地址族为 IPv4，
        # SOCK_STREAM 指定类型为面向连接的流式传输（TCP）。内核会分配发送/接收缓冲区并返回套接字对象。
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        #设置套接字的 超时定时器
        s.settimeout(timeout)
        #connect_ex()：无论成功失败都不抛异常，而是返回 errno（成功为 0，
        # 失败如 Connection refused 返回 10061，超时返回 10060）
        '''
        对，*connect_ex 就是在执行 TCP 三次握手*：
        1. 客户端 → 服务端：发送 SYN 报文
        2. 服务端 → 客户端：回复 SYN+ACK
        3. 客户端 → 服务端：回复 ACK
        三次握手完成 → 连接建立 → connect_ex 返回 0。
        如果端口没在监听，服务端内核直接回 RST 报文，三次握手中途终止，connect_ex 返回非零值（如 10061），不用等超时。
        '''
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        return False


def _mark_failed(reason: str) -> None:
    """记录一次连接失败，并告诉调用方多久后会再试。

    为什么要记时间而不是只置一个 False：
        Redis 重启、容器重建、网络抖动恢复之后，进程不该要求人工重启才能重新用上缓存。
        冷却 30s 是为了别让每个请求都去连一个死掉的服务（连接超时本身就是成本）。
    """
    global _last_failure_at
    _last_failure_at = time.monotonic()
    logger.warning(f"[Cache] {reason}，缓存已降级（{_RETRY_INTERVAL:.0f}s 后重试）")


def _init():
    global _redis, _ENABLED, _CHECKED
    if _ENABLED:
        return
    # 失败过一次就等冷却期结束再试，避免每个请求都去连一次死掉的服务
    if _CHECKED and time.monotonic() - _last_failure_at < _RETRY_INTERVAL:
        return
    _CHECKED = True
    #redis 库是 Python 与 Redis 服务通信的客户端驱动。 它实现了 RESP（REdis Serialization Protocol）协议，
    # 把 Python 函数调用翻译成 Redis 能理解的命令报文，再解析返回的数据。
    import redis

    # 容器 / 云环境给的是 URL（redis://redis:6379/0），本地默认走 host+port。
    # URL 优先：docker-compose 里服务名解析、带密码、选 db 都靠它，
    # 拆成 HOST/PORT/PASSWORD 三个变量表达不了这些
    url = os.getenv("ZST_REDIS_URL", "").strip()
    if url:
        try:
            _redis = redis.from_url(
                url,
                socket_connect_timeout=1,
                socket_timeout=1,
                decode_responses=True,
            )
            _redis.ping()
            _ENABLED = True
            logger.info("[Cache] Redis 已连接（来自 ZST_REDIS_URL）")
        except Exception as exc:
            _mark_failed(f"Redis 连接失败（{exc.__class__.__name__}）")
        return

    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", 6379))
    # 本地才做端口探活：连接 127.0.0.1 失败要等 TCP 超时，
    # 而容器网络里主机名不存在是立即失败，没必要多这一次握手
    if not _check_port_open(host, port):
        _mark_failed("Redis 未启动")
        return
    try:
        _redis = redis.Redis(host=host, port=port, socket_connect_timeout=2, decode_responses=True)
        _redis.ping()
        _ENABLED = True
        logger.info("[Cache] Redis 已连接")
    except Exception as exc:
        _mark_failed(f"Redis 连接失败（{exc.__class__.__name__}）")


def get_client():
    """返回可用的 redis 客户端，不可用时返回 None。

    给上层的「结构化缓存」（JSON / TTL / 批量失效）复用同一个连接池，
    避免一个进程里开两套客户端——两套就有两套重连策略，故障时行为不一致。
    """
    _init()
    return _redis if _ENABLED else None


def get_cache(key: str) -> str | None:
    _init()
    if not _ENABLED or _redis is None:
        return None
    try:
        #向redis服务端取缓存的全部响应过程
        '''
      Python 线程
      ↓ 调用 .get("rag:xxx")
    redis-py 内部
      ↓ RESP 序列化: *2\r\n$3\r\nGET\r\n$8\r\nrag:xxx\r\n
      ↓ 写入 socket buffer
    内核
      ↓ TCP 协议栈封装成数据包
      ↓ 通过网卡发到 Redis 服务端（本地则走 loopback）
    
    Redis 服务端
      ↓ 收到 GET 命令
      ↓ 从内存字典中按 key 查找
      ↓ 找到 → 返回 value；找不到 → 返回 nil（None）
      ↓ RESP 序列化回复
    
    你的代码收到结果
      ↓ decode_responses → str（或 None）
        '''
        return _redis.get(key)
    except Exception:
        return None


def set_cache(key: str, value: str, ttl: int = 300):
    _init()
    if not _ENABLED or _redis is None:
        return
    try:
        _redis.setex(key, ttl, value)
    except Exception:
        pass


def delete_cache(key: str):
    _init()
    if not _ENABLED or _redis is None:
        return
    try:
        _redis.delete(key)
    except Exception:
        pass


def is_enabled() -> bool:
    if not _CHECKED:
        _init()
    return _ENABLED
