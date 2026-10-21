"""对话并发闸门（bulkhead）。

为什么需要它
────────────────────────────────────────
压测里 100 个并发对话把服务打死的顺序是这样的：

    RAG 检索排队（重排是 CPU 密集，2 核跑不动）
      → anyio 工作线程池（默认 40）被占满
      → 连「查产品列表」这种不吃模型的请求也开始超时
      → DashScope 侧并发上来后 429
      → 请求堆积，内存涨到 1.5GB
      → 容器被 OOM kill

注意这里没有一步是「代码写错了」。每一步都是「来多少接多少」的必然结果。
HTTP 服务不加背压，过载时表现不是变慢，而是**整体雪崩**：
少数几个慢请求吃光所有资源，剩下 90 个本来能秒回的请求一起超时。

所以这道闸门的作用不是提速，而是**把过载限制在少数请求上**：
    - 同时最多 N 个对话在跑（默认 16）；
    - 排不上队的请求等 3 秒就返回 503 + Retry-After，让客户端退避；
    - 排队不占线程、不建连接、不碰模型，成本接近零。

为什么限 16 而不是 100
────────────────────────────────────────
LLM 生成是 IO 等待（不吃 CPU），RAG 重排是 CPU 密集型。
2C4G 上重排闸门（rag/reranker.py）默认只放 2 个并发，
再往上堆对话数，只会让每个对话的检索段排更久。

16 这个值的含义是「允许同时在等待 LLM 的对话数」：
按单轮 60~80 秒算，理论吞吐 16/70 ≈ 0.23 QPS。
要更高吞吐，路径是把实例数从 1 加到 N（见 docker-compose 的 replicas），
而不是把这个数调大——单进程的 CPU 上限是物理的。
"""
from __future__ import annotations

import asyncio
import os
import threading

from utils.logger_handler import logger


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning(f"[ChatGate] 环境变量 {name}={raw!r} 不是整数，使用默认值 {default}")
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning(f"[ChatGate] 环境变量 {name}={raw!r} 不是数字，使用默认值 {default}")
        return default


class ChatBusy(RuntimeError):
    """排队超时。路由层要把它翻译成 503 + Retry-After。"""

    def __init__(self, message: str, retry_after: int = 5) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Ticket:
    """一次占用的凭证。release 幂等——流正常结束、抛异常、客户端断开会各调一次。"""

    __slots__ = ("_gate", "_released")

    def __init__(self, gate: "ChatGate") -> None:
        self._gate = gate
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate._release()  # noqa: SLF001 —— 同模块内的内部协作


class ChatGate:
    def __init__(self) -> None:
        self.enabled = os.getenv("ZST_CHAT_GATE_ENABLED", "1").strip() not in ("0", "false", "False")
        self.limit = _env_int("ZST_CHAT_CONCURRENCY", 16)
        self.queue_timeout = _env_float("ZST_CHAT_QUEUE_TIMEOUT", 3.0)
        self.retry_after = _env_int("ZST_CHAT_RETRY_AFTER", 5)

        self._sem: asyncio.Semaphore | None = None
        self._sem_loop: asyncio.AbstractEventLoop | None = None
        self._active = 0
        self._waiting = 0
        self._rejected = 0
        self._completed = 0
        self._stats_lock = threading.Lock()

    def _semaphore(self) -> asyncio.Semaphore:
        """按事件循环懒建信号量。

        为什么不能在建对象时就建 asyncio.Semaphore：
        Semaphore 会绑定创建它的事件循环，而这个对象是模块级单例。
        测试里每个用例新建一个 loop、uvicorn 在另一个 loop 里跑，
        跨 loop 用同一个 Semaphore 会抛 "attached to a different loop"。
        """
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self.limit)
            self._sem_loop = loop
        return self._sem

    async def acquire(self) -> Ticket:
        """占一个对话位。排队超过 queue_timeout 抛 ChatBusy。

        必须在返回 StreamingResponse **之前**调用：
        一旦响应头发出去（200），再想说「服务忙」只能中途断流，
        前端拿到的是半截回答，比 503 难处理得多。
        """
        if not self.enabled:
            return Ticket(self)

        sem = self._semaphore()
        with self._stats_lock:
            self._waiting += 1
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self.queue_timeout)
        except asyncio.TimeoutError as exc:
            with self._stats_lock:
                self._rejected += 1
            raise ChatBusy(
                f"当前对话请求过多（上限 {self.limit}），请 {self.retry_after} 秒后重试",
                retry_after=self.retry_after,
            ) from exc
        finally:
            with self._stats_lock:
                self._waiting -= 1

        with self._stats_lock:
            self._active += 1
        return Ticket(self)

    def _release(self) -> None:
        if not self.enabled:
            return
        with self._stats_lock:
            self._active = max(0, self._active - 1)
            self._completed += 1
        if self._sem is not None:
            try:
                self._sem.release()
            except RuntimeError:
                # loop 已关闭（进程正在退出）：忽略，否则退出阶段会刷一堆噪音
                pass

    def snapshot(self) -> dict:
        with self._stats_lock:
            return {
                "enabled": self.enabled,
                "limit": self.limit,
                "active": self._active,
                "waiting": self._waiting,
                "rejected": self._rejected,
                "completed": self._completed,
            }


gate = ChatGate()
