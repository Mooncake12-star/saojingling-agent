"""进程内 RAG 单例 + rag_summarize 的"本地优先"实现。

为什么要有这个模块：
MCP 子进程要实现 rag_summarize 就得自己 import torch + transformers 并加载
BGE 重排模型（约 1.5GB 常驻）。主进程这边已经加载了向量库，8GB 内存的机器
上两份叠加会把系统打进重度换页，子进程直接被换出成"假死"（表现为调用永久
卡死、无任何报错）。

所以模型全系统只保留一份，放在主进程；MCP 侧只保留工具定义。
FastAPI（fastapi_app/deps.py）和 Streamlit（app.py）两个入口都要注册。

P3 起「一份服务」变成了「每个检索范围一份」：
    RAG 服务在构造时就把候选集合（向量库 + BM25 索引）定死了（见 rag_service.py），
    所以不同用户 / 不同知识库不能共用一个实例，否则隔离形同虚设。
    这里按 RagScope.cache_key 缓存，并限制缓存数量——每份实例都持有一个
    LLM 客户端和一份 BM25 索引，无上限缓存迟早把内存吃光。

并发模型（这一步是压测之后才改对的）
────────────────────────────────────────
改造前：整个 `get_rag_service` 用一把全局锁包住，包括 `_build()`。
而 `_build()` 要打开 Chroma、拉范围内全部 chunk、建 BM25 索引，冷启动 1~3 秒；
首次检索还要顺带加载重排模型 40~60 秒。

于是一个用户首访、正在建索引时，**其他所有用户**（哪怕他们的 scope 早已缓存好、
本该走快路径）都被挡在锁外面。压测里 3 个用户分别 58.5s / 66.8s / 71.6s，
严格串行——不是模型慢，是大家在排队等同一把锁。

现在的结构：
    快路径        -> 读缓存（dict 锁，纳秒级），命中直接返回
    同一 scope     -> 用 Event 去重，只有一个线程构造，其余等待（本应共用一份）
    不同 scope     -> 互不阻塞，各自并行构造
    全局闸门      -> 同时最多 N 个构造在跑（默认 2），防止 8 个 scope 一起
                     拉全量 chunk 把 2 核 CPU 打满
    等待超时      -> 抛 RagBuildBusy，由调用方返回「正在初始化，请稍后重试」，
                     而不是让用户挂 80 秒后再 504
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

from rag.scope import RagScope, current_scope
from utils.logger_handler import logger


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning(f"[LocalTool] 环境变量 {name}={raw!r} 不是整数，使用默认值 {default}")
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning(f"[LocalTool] 环境变量 {name}={raw!r} 不是数字，使用默认值 {default}")
        return default


# key = (user_id, knowledge_base_id)；None 表示「全局范围」（脚本 / Streamlit 旧路径）
# OrderedDict：命中时 move_to_end，淘汰时 popitem(last=False) —— 真正的 LRU。
# 原来用普通 dict + next(iter(...)) 淘汰的是「最早插入」而不是「最久未用」，
# 正好会把还在频繁访问的那份服务踢掉，下一轮又重建一次。
_services: OrderedDict[object, object] = OrderedDict()

# 保护 _services / _loading 的结构本身，**绝不**在持有它的时候做构造
_state_lock = threading.Lock()

# key -> Event：表示「这个 key 正在被某个线程构造」。等待者只等 Event，不持有任何锁
_loading: dict[object, threading.Event] = {}

# 同时最多几个 scope 在构造。每份构造都要 Chroma 全量拉取 + BM25 建索引，
# 2C4G 上并行太多只会让每个都变慢
_build_gate = threading.BoundedSemaphore(_env_int("ZST_RAG_BUILD_CONCURRENCY", 2))

# 最多缓存多少份。按「每个用户一份默认范围 + 若干个知识库」估算
MAX_SCOPES = _env_int("ZST_RAG_MAX_SCOPES", 8)

# 等别人构造完的最长时间。超时说明系统正在过载，快速失败比挂着更好
BUILD_WAIT_TIMEOUT = _env_float("ZST_RAG_BUILD_WAIT_TIMEOUT", 30.0)


class RagBuildBusy(RuntimeError):
    """排队等 RAG 服务构造超时。调用方应把它翻译成「稍后重试」。"""


def _build(scope: RagScope | None):
    from rag.rag_service import RagSummarizeService

    where = scope.to_where() if scope is not None else None
    logger.info(f"[LocalTool] 正在初始化 RAG 服务 where={where}（加载重排模型）...")
    service = RagSummarizeService(where=where)
    logger.info(f"[LocalTool] RAG 服务初始化完成 where={where}")
    return service


def get_rag_service(scope: RagScope | None = None):
    """按范围取 RAG 服务，惰性创建。

    不传 scope 时用「当前请求范围」；请求上下文里也没有，就走全局范围。
    全局范围只该出现在没有用户身份的场景（命令行、评测脚本、Streamlit 旧界面）。
    """
    if scope is None:
        scope = current_scope()
    key = scope.cache_key if scope is not None else None

    # ── 快路径：已缓存，连构造闸门都不碰
    with _state_lock:
        cached = _services.get(key)
        if cached is not None:
            _services.move_to_end(key)
            return cached
        owner_event = _loading.get(key)
        if owner_event is None:
            # 我是第一个：登记「正在构造」，随即释放锁去做真正的工作
            owner_event = threading.Event()
            _loading[key] = owner_event
            i_am_owner = True
        else:
            i_am_owner = False

    if not i_am_owner:
        # 同一个 key 已经有人在建：等它，而不是自己再建一份（重复建索引 = 内存翻倍）
        if owner_event.wait(BUILD_WAIT_TIMEOUT):
            with _state_lock:
                cached = _services.get(key)
                if cached is not None:
                    _services.move_to_end(key)
                    return cached
            # 被唤醒但没有缓存 = owner 构造失败了。快速失败，别让用户再等一轮
            raise RagBuildBusy("知识库初始化失败，请稍后重试")
        raise RagBuildBusy(
            f"知识库初始化中（等待超过 {BUILD_WAIT_TIMEOUT:.0f}s），请稍后重试"
        )

    # ── 慢路径：我是 owner
    acquired = False
    try:
        # 构造闸门：限制同时构造的 scope 数。等待期间不持有 _state_lock，
        # 其他 key 的快路径不受影响
        acquired = _build_gate.acquire(timeout=BUILD_WAIT_TIMEOUT)
        if not acquired:
            raise RagBuildBusy(
                f"知识库初始化排队超时（>{BUILD_WAIT_TIMEOUT:.0f}s），请稍后重试"
            )

        started = time.perf_counter()
        service = _build(scope)
        cost = time.perf_counter() - started

        with _state_lock:
            _services[key] = service
            _services.move_to_end(key)
            while len(_services) > MAX_SCOPES:
                evicted_key, _ = _services.popitem(last=False)
                logger.info(f"[LocalTool] RAG 服务缓存超上限（{MAX_SCOPES}），淘汰最久未用 key={evicted_key}")
        logger.info(
            "[LocalTool] RAG 服务已缓存",
            extra={"context": {"event": "rag.build", "cost_s": round(cost, 3),
                               "cached": len(_services)}},
        )
        return service
    finally:
        if acquired:
            _build_gate.release()
        # 无论成功失败都要唤醒等待者：成功的话它们从缓存拿，失败的话它们拿到 None 去抛错，
        # 不能让人等到超时
        with _state_lock:
            _loading.pop(key, None)
        owner_event.set()


def clear_rag_cache() -> None:
    """清空缓存（测试与热更新用）。"""
    with _state_lock:
        _services.clear()
        _loading.clear()


def warmup(scope: RagScope | None = None) -> bool:
    """预热：启动阶段后台调用，把「第一个用户等 60 秒」变成「部署时等一次」。

    失败只记日志，不抛：预热是优化，不该让服务起不来。
    """
    started = time.perf_counter()
    try:
        get_rag_service(scope)
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.warning(f"[LocalTool] RAG 预热失败: {exc}")
    logger.info(
        "[LocalTool] RAG 预热结束",
        extra={"context": {"event": "rag.warmup", "ok": ok,
                           "cost_s": round(time.perf_counter() - started, 2)}},
    )
    return ok


def local_rag_summarize(arguments: dict) -> str:
    """rag_summarize 的本地实现（由 Agent 在自己的线程池里调用）。

    注意这里**不接受** knowledge_base_id 参数：检索范围来自请求上下文，
    不来自模型生成的工具参数。让模型决定查哪份数据，
    等于把隔离交给一个可能被提示注入影响的组件。
    """
    query = (arguments or {}).get("query", "")
    if not query:
        return "参数错误：缺少 query 字段"
    try:
        return get_rag_service().rag_summarize(query)
    except RagBuildBusy as e:
        # 过载时的正确姿势：告诉模型「稍后再试」，让它给用户一句可理解的回复，
        # 而不是抛 500 或让请求挂 80 秒
        logger.warning(f"[LocalTool] 检索服务繁忙: {e}")
        return f"知识库正在初始化，请稍后重试。（{e}）"
    except Exception as e:
        logger.error(f"[LocalTool] 本地 RAG 检索失败: {e}")
        return f"知识库检索失败：{e}"
