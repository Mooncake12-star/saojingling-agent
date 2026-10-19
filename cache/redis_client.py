import os
#socket：Python 内置的网络通信标准库，提供底层网络接口。这里用它做 端口探活
import socket

from utils.logger_handler import logger

_redis = None
_ENABLED = False
_CHECKED = False


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


def _init():
    global _redis, _ENABLED, _CHECKED
    if _CHECKED:
        return
    _CHECKED = True
    #redis 库是 Python 与 Redis 服务通信的客户端驱动。 它实现了 RESP（REdis Serialization Protocol）协议，
    # 把 Python 函数调用翻译成 Redis 能理解的命令报文，再解析返回的数据。
    import redis
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", 6379))
    if not _check_port_open(host, port):
        logger.warning("[Cache] Redis 未启动，缓存已降级（跳过）")
        return
    try:
        _redis = redis.Redis(host=host, port=port, socket_connect_timeout=2, decode_responses=True)
        _redis.ping()
        _ENABLED = True
        logger.info("[Cache] Redis 已连接")
    except Exception:
        logger.warning("[Cache] Redis 连接失败，缓存已降级（跳过）")


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
