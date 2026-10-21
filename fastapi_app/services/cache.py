"""缓存层：只做一件事——把「读多写少」的视图塞进 Redis。

三条原则，比代码本身重要：

1. **缓存是加速器，不是依赖**
   Redis 挂了、超时了、没部署，业务必须照常返回，只是慢一点。
   所以每个操作都 try/except 后降级为「没有缓存」，绝不让异常冒到路由层。
   反过来做（把 Redis 当数据库用）会让一次缓存故障变成一次全站故障。

2. **只缓存可序列化的视图，不缓存 ORM 对象**
   Session 一关，ORM 对象就是 detached 状态，访问未加载的属性直接抛
   `DetachedInstanceError`。缓存的东西必须自带全部字段（这里用 Pydantic 导出）。

3. **键里必须有 user_id**
   缓存和数据库一样是数据面：键设计错了，就是另一条越权通道。
   `zst:v1:kb:list:{user_id}` 让 A 的缓存物理上不可能被 B 读到。

这里没有做的（知道该怎么做，只是当前不值得做）：
    - 缓存击穿：热点 key 过期瞬间大量请求打到库 → 需要互斥锁/单飞
    - 缓存雪崩：大量 key 同时过期 → 已用 TTL 抖动缓解一部分
    - 缓存穿透：查不存在的数据 → 空结果也缓存，用较短 TTL
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Protocol

from utils.logger_handler import logger

# 版本前缀：将来改了列表的返回结构，把 v1 换成 v2 就等于全量失效，
# 不用写迁移脚本去删旧 key
KB_LIST_PREFIX = "zst:v1:kb:list:"

# 60s 是「用户能容忍的滞后」和「数据库压力」之间的折中：
# 太短等于没缓存，太长会让用户建完库刷新看不到（虽然写路径已失效，但多实例下会滞后）
KB_LIST_TTL = 60
# 抖动上限：所有 key 都是同一时刻写入的，若不抖动会在同一秒集体过期
KB_LIST_TTL_JITTER = 8


def kb_list_key(user_id: int) -> str:
    return f"{KB_LIST_PREFIX}{user_id}"


def kb_list_ttl(payload: list[Any]) -> int:
    """空列表也缓存（防穿透），但不需要缓存太久。

    为什么敢缓存空列表：写路径一定会失效，不会出现「建了库却看不到」。
    """
    base = KB_LIST_TTL if payload else 10
    return base + random.randint(0, KB_LIST_TTL_JITTER)


class Cache(Protocol):
    """缓存的最小接口。用 Protocol 是为了让测试能塞一个假实现，不用继承。"""

    def get_json(self, key: str) -> Any | None: ...
    def set_json(self, key: str, value: Any, ttl: int) -> None: ...
    def delete(self, *keys: str) -> None: ...
    def backend(self) -> str: ...


class NullCache:
    """不缓存。默认实现，测试和「没配 Redis」的环境都走它。"""

    def get_json(self, key: str) -> Any | None:
        return None

    def set_json(self, key: str, value: Any, ttl: int) -> None:
        return None

    def delete(self, *keys: str) -> None:
        return None

    def backend(self) -> str:
        return "off"


class RedisCache:
    """Redis 实现，带简易熔断。

    熔断的意义：Redis 挂掉时，每个请求都要等一次 connect timeout（1s），
    100 QPS 就是 100 个线程卡在等超时。所以失败后 5 秒内直接当没有缓存，
    让请求立刻走数据库——牺牲 5 秒的缓存能力，换回全部请求的响应时间。

    客户端是**懒获取**的：`cache/redis_client.py` 只在第一次用时连一次，
    连不上就永久降级。所以这里每次操作都调一次 client_factory（很便宜），
    而不是在构造时把客户端钉死。
    """

    def __init__(self, client_factory=None, cooldown: float = 5.0) -> None:
        self._client_factory = client_factory or _default_client_factory
        self._cooldown = cooldown
        self._open_until = 0.0  # monotonic 时间；> now 表示熔断中
        self._failures = 0

    # ---------- 内部 ----------

    def _client(self):
        if time.monotonic() < self._open_until:
            return None  # 熔断中，直接当没有缓存
        return self._client_factory()

    def _trip(self, action: str, exc: Exception) -> None:
        self._failures += 1
        self._open_until = time.monotonic() + self._cooldown
        # 只在熔断的这一刻打日志：出错期间可能每秒上千次，
        # 每次都记会把真正有用的日志冲掉
        logger.warning(
            f"[Cache] {action} 失败，降级 {self._cooldown:.0f}s 后再试: {exc.__class__.__name__}: {exc}"
        )

    # ---------- 接口 ----------

    def get_json(self, key: str) -> Any | None:
        client = self._client()
        if client is None:
            return None
        try:
            raw = client.get(key)
        except Exception as exc:  # noqa: BLE001 - 缓存故障不能影响业务
            self._trip("读取", exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # 脏数据（版本升级遗留、人工写坏）：当成 miss，顺手删掉
            logger.warning(f"[Cache] 键 {key} 内容不是合法 JSON，已删除")
            self.delete(key)
            return None

    def set_json(self, key: str, value: Any, ttl: int) -> None:
        client = self._client()
        if client is None:
            return
        try:
            # default=str 兜底 datetime 之类的类型，宁可存成字符串也不让写缓存失败
            client.setex(key, ttl, json.dumps(value, ensure_ascii=False, default=str))
        except Exception as exc:  # noqa: BLE001
            self._trip("写入", exc)

    def delete(self, *keys: str) -> None:
        if not keys:
            return
        client = self._client()
        if client is None:
            return
        try:
            client.delete(*keys)
        except Exception as exc:  # noqa: BLE001
            self._trip("删除", exc)

    def backend(self) -> str:
        """健康检查要看的是「缓存现在到底能不能用」，不是「我配置了 Redis」。

        所以这里真的去拿一次客户端（`redis_client` 内部有 30s 冷却 +
        连接池复用，不会每次都建连接）。返回四种状态：
            redis               正常
            redis(circuit-open) 刚失败过，冷却期内不再尝试
            redis(unavailable)  连不上（域名/端口/鉴权问题）
            off                 根本没配（NullCache）

        早期版本只看熔断标记，结果「从没连上过」也报 redis——
        健康检查撒谎比没有健康检查更糟。
        """
        if time.monotonic() < self._open_until:
            return "redis(circuit-open)"
        if self._client_factory() is None:
            return "redis(unavailable)"
        return "redis"


def _default_client_factory():
    # 延迟到调用时 import：没装 redis 库的环境（纯单元测试）也能 import 本模块
    from cache.redis_client import get_client

    return get_client()


# 模块级单例。默认 NullCache：没显式配置就当没有缓存，
# 这样单元测试不会因为「本机没起 Redis」而变慢或变不稳定
_cache: Cache = NullCache()


def get_cache() -> Cache:
    return _cache


def configure(cache: Cache) -> None:
    global _cache
    _cache = cache


def configure_from_env() -> Cache:
    """启动时调用：有 ZST_REDIS_URL 就启用 Redis，否则保持 Null。

    不自动探测 localhost:6379——隐式行为会让「为什么本地慢、线上快」
    变成一件靠猜的事。开关显式写在配置里。
    """
    import os

    if os.getenv("ZST_REDIS_URL", "").strip():
        configure(RedisCache())
        logger.info("[Cache] 已启用 Redis 缓存")
    else:
        logger.info("[Cache] 未配置 ZST_REDIS_URL，缓存关闭")
    return get_cache()
