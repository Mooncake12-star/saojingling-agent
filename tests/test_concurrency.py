"""并发与降级测试：把「过载时不雪崩」这件事固定成断言。

为什么这些测试必须存在
────────────────────────────────────────
并发问题在功能测试里完全看不出来：单个请求跑得好好的，100 个一起来才崩，
而且崩的方式是「所有请求一起变慢」，不是某一处报错。这类 bug 修完如果没有
测试守着，下一次改动（哪怕只是把一把锁挪个位置）会悄悄退回去，
而回归只会在压测时暴露——那时候已经上线了。

四个不变量：
    1. 重排模型全进程只加载一次（重复加载 = 内存翻倍 = OOM）
    2. 模型加载期间请求立刻降级，不排队等锁（否则 100 并发串行）
    3. 不同检索范围各自并行构造，相同范围只构造一次
    4. 对话超过并发上限后快速返回 503 + Retry-After，而不是挂 80 秒
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import types

import pytest

import rag.local_tool as local_tool
from fastapi_app.concurrency import ChatBusy, ChatGate
from rag.reranker import (
    STATE_READY,
    Reranker,
)
from rag.scope import RagScope


def run_async(coro):
    """在同步测试里跑协程。

    不引入 pytest-asyncio：项目其余测试都是同步的，为一个文件加一个插件
    会让「装依赖才能跑测试」这件事多一个坑。
    """
    return asyncio.run(coro)


# ══════════════════════════════════════════
# 1. Reranker：只加载一次
# ══════════════════════════════════════════
class _FakeCrossEncoderModule(types.ModuleType):
    """假的 sentence-transformers：记录构造次数，构造时故意慢一点。

    慢一点是关键——竞态窗口越小越难复现。真实场景里加载要 40 秒，
    窗口极大；测试里用 50ms 已经足够让 20 个线程全部挤进来。
    """

    def __init__(self, delay: float = 0.05):
        super().__init__("sentence_transformers")
        self.construct_calls = 0
        self._lock = threading.Lock()
        self._delay = delay

        outer = self

        class CrossEncoder:
            def __init__(self, _name):
                with outer._lock:
                    outer.construct_calls += 1
                time.sleep(outer._delay)

            def predict(self, pairs):
                return [0.5 for _ in pairs]

        self.CrossEncoder = CrossEncoder


def test_reranker_loads_model_only_once(monkeypatch):
    """20 个线程同时 warmup，模型只能被构造一次。

    改造前的实现没有锁：每个线程都看到「还没加载」，于是各自 new 一个
    CrossEncoder。实测内存从 700MB 涨到 1.53GB，随后进入换页，
    所有请求表现为「不报错也不返回」。
    """
    fake = _FakeCrossEncoderModule()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    reranker = Reranker()
    results: list[bool] = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()  # 20 个线程尽量同时冲进 warmup
        results.append(reranker.warmup())

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert fake.construct_calls == 1, f"模型被加载了 {fake.construct_calls} 次，必须只有 1 次"
    assert reranker.state == STATE_READY


def test_reranker_score_skips_while_loading(monkeypatch):
    """加载期间 score 立刻返回 None，不阻塞调用线程。

    这是「加载 40 秒」和「服务不可用 40 秒」的分界线：
    等待方一旦阻塞，40 个 anyio 工作线程会在几秒内被占满，
    连不碰模型的接口都开始超时。
    """
    fake = _FakeCrossEncoderModule(delay=0.6)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setenv("ZST_RERANK_READY_TIMEOUT", "0.05")

    reranker = Reranker()
    loader = threading.Thread(target=reranker.warmup, daemon=True)
    loader.start()
    # 等它进入「正在构造」状态，而不是依赖固定 sleep
    deadline = time.time() + 2
    while fake.construct_calls == 0 and time.time() < deadline:
        time.sleep(0.005)

    started = time.perf_counter()
    scores = reranker.score([["q", "passage"]])
    cost = time.perf_counter() - started

    # 事件断言而非时间断言：score 返回时构造线程必须还活着——
    # 这直接证明「没等模型加载完」。绝对时间断言（如 cost < 0.3）
    # 在 CI 的 2 核慢机器上会随机失败，是典型的 flaky test。
    assert scores is None, "模型未就绪时必须降级"
    assert loader.is_alive(), "score 返回时加载还没完成，说明确实没等它"
    assert cost < 5, f"降级路径花了 {cost:.2f}s，疑似死锁"
    assert reranker.snapshot()["score_skipped_not_ready"] >= 1

    loader.join(timeout=5)
    assert reranker.state == STATE_READY


def test_reranker_gate_skips_when_busy(monkeypatch):
    """推理并发满了就跳过精排，而不是排队。

    CPU 密集型推理排队没有收益：2 核上同时跑 8 个 CrossEncoder，
    总吞吐不变，单次延迟翻几倍，还占着调用方的线程。
    """
    fake = _FakeCrossEncoderModule(delay=0.0)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setenv("ZST_RERANK_CONCURRENCY", "1")
    monkeypatch.setenv("ZST_RERANK_GATE_TIMEOUT", "0.05")

    reranker = Reranker()
    assert reranker.warmup() is True

    # 直接占用唯一的闸门，模拟「另一个请求正在推理」
    held = reranker._gate.acquire(timeout=1)
    assert held is True
    try:
        started = time.perf_counter()
        scores = reranker.score([["q", "p"]])
        cost = time.perf_counter() - started
    finally:
        reranker._gate.release()

    assert scores is None
    # 闸门超时是 0.05s，正常路径必然远小于它；上限放宽到 1s 只防死锁，
    # 避免在 CI 慢机器上因调度抖动误报（绝对时间断言是 flaky 的根源）
    assert cost < 1.0, f"闸门超时 0.05s 却花了 {cost:.2f}s，疑似在排队而非跳过"
    assert reranker.snapshot()["score_skipped_busy"] == 1


# ══════════════════════════════════════════
# 2. RAG 服务缓存：不同范围并行、相同范围去重
# ══════════════════════════════════════════
@pytest.fixture()
def clean_rag_cache(monkeypatch):
    """每个用例都从空缓存开始，避免上一个用例的服务被复用导致计时失真。"""
    local_tool.clear_rag_cache()
    monkeypatch.setattr(local_tool, "BUILD_WAIT_TIMEOUT", 10.0)
    yield
    local_tool.clear_rag_cache()


def test_different_scopes_build_in_parallel(clean_rag_cache, monkeypatch):
    """两个不同用户首次访问，构造是并行的，不是排队的。

    改造前整个 get_rag_service 被一把全局锁包住：A 在建索引（1~3 秒，
    冷启动含模型是 40~60 秒），B 哪怕用的是完全无关的数据，也只能等——
    压测里 3 个用户 58.5s / 66.8s / 71.6s 严格串行就是这么来的。
    """
    built: list[int] = []
    build_lock = threading.Lock()
    spans: list[tuple[int, float, float]] = []  # (user_id, start, end)

    def fake_build(scope):
        # 记录每个构造的起止区间，之后断言两个区间重叠——
        # 这比「总耗时 < 0.6s」可靠：sleep 不吃 CPU，重叠窗口在
        # 2 核 CI 机器上同样成立，而绝对时间断言会因调度抖动误报
        start = time.perf_counter()
        time.sleep(0.8)  # 模拟拉 chunk + 建 BM25 索引，加大重叠窗口
        end = time.perf_counter()
        with build_lock:
            built.append(scope.user_id)
            spans.append((scope.user_id, start, end))
        return object()

    monkeypatch.setattr(local_tool, "_build", fake_build)

    threads = [
        threading.Thread(target=local_tool.get_rag_service, args=(RagScope(user_id=uid),))
        for uid in (1, 2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(built) == [1, 2]
    # 串行时后一个的 start 必然晚于前一个的 end；并行则两个区间重叠
    _, s1, e1 = spans[0]
    _, s2, e2 = spans[1]
    overlap = max(s1, s2) < min(e1, e2)
    assert overlap, (
        "两个范围的构造区间没有重叠，说明仍然串行："
        f"spans={[(round(s,2), round(e,2)) for _, s, e in spans]}"
    )


def test_same_scope_builds_only_once(clean_rag_cache, monkeypatch):
    """同一个范围被 10 个线程同时请求，只构造一次。

    重复构造不只是浪费 CPU：每份实例都持有一份 BM25 索引和候选文档列表，
    内存会按「并发数」而不是「用户数」增长。
    """
    calls = {"n": 0}
    lock = threading.Lock()

    def fake_build(scope):
        time.sleep(0.2)
        with lock:
            calls["n"] += 1
        return object()

    monkeypatch.setattr(local_tool, "_build", fake_build)

    scope = RagScope(user_id=7, knowledge_base_id=3)
    results: list[object] = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        results.append(local_tool.get_rag_service(scope))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert calls["n"] == 1, f"同一范围构造了 {calls['n']} 次"
    assert len(results) == 10
    assert all(r is results[0] for r in results), "所有线程必须拿到同一个实例"


def test_build_wait_timeout_fails_fast(clean_rag_cache, monkeypatch):
    """排队超时抛 RagBuildBusy，让调用方回一句「稍后重试」。

    宁可告诉用户「忙」，也不要让他挂 80 秒后收到 504——
    超时对用户来说是「不知道发生了什么」，503 至少是可退避的信号。
    """
    monkeypatch.setattr(local_tool, "BUILD_WAIT_TIMEOUT", 0.1)

    owner_started = threading.Event()

    def slow_build(scope):
        # 先通知「我已经是 owner、正在构造」，消除主线程与 holder
        # 的启动竞态（固定 sleep(0.05) 在慢机器上可能等不到它成为 owner）
        owner_started.set()
        time.sleep(0.5)
        return object()

    monkeypatch.setattr(local_tool, "_build", slow_build)

    scope = RagScope(user_id=9)
    holder = threading.Thread(target=local_tool.get_rag_service, args=(scope,), daemon=True)
    holder.start()
    assert owner_started.wait(timeout=5), "holder 线程 5s 内没有进入构造状态"

    started = time.perf_counter()
    with pytest.raises(local_tool.RagBuildBusy):
        local_tool.get_rag_service(scope)
    cost = time.perf_counter() - started

    # 正确实现应在 BUILD_WAIT_TIMEOUT(0.1s) 附近放弃；上限 2s 只防死锁，
    # 绝对贴近值（cost < 0.4）在 CI 慢机器上会因调度抖动误报
    assert cost < 2.0, f"排队超时 0.1s 却花了 {cost:.2f}s，疑似永远等待"
    holder.join(timeout=5)


# ══════════════════════════════════════════
# 3. 对话闸门
# ══════════════════════════════════════════
def test_gate_rejects_after_queue_timeout(monkeypatch):
    """占满并发位后，新请求等 queue_timeout 就抛 ChatBusy。"""
    monkeypatch.setenv("ZST_CHAT_CONCURRENCY", "1")
    monkeypatch.setenv("ZST_CHAT_QUEUE_TIMEOUT", "0.05")
    gate = ChatGate()

    async def scenario():
        first = await gate.acquire()
        try:
            started = time.perf_counter()
            with pytest.raises(ChatBusy) as excinfo:
                await gate.acquire()
            cost = time.perf_counter() - started
            return cost, excinfo.value
        finally:
            first.release()

    cost, exc = run_async(scenario())

    # 正确实现等 queue_timeout(0.05s) 就放弃；上限 1s 只防死锁，
    # 避免绝对时间断言在 CI 慢机器上因调度抖动误报
    assert cost < 1.0, f"应该等 {gate.queue_timeout}s 就放弃，实际 {cost:.2f}s"
    assert exc.retry_after == gate.retry_after
    assert gate.snapshot()["rejected"] == 1


def test_gate_release_is_idempotent(monkeypatch):
    """release 可能被调用两次（生成器 finally + 响应 background），不能多还位。

    多还一位的后果是信号量计数超过上限，闸门形同虚设——
    过载保护静默失效，比没有保护更危险。
    """
    monkeypatch.setenv("ZST_CHAT_CONCURRENCY", "1")
    gate = ChatGate()

    async def scenario():
        ticket = await gate.acquire()
        ticket.release()
        ticket.release()  # 重复释放
        # 位子应该已经还回去了，能再拿一次
        again = await gate.acquire()
        again.release()
        return gate.snapshot()

    snap = run_async(scenario())
    assert snap["active"] == 0
    assert snap["completed"] >= 1


def test_gate_disabled_when_env_set(monkeypatch):
    """显式关闭时不限流（本地调试用），但仍然返回可 release 的票据。"""
    monkeypatch.setenv("ZST_CHAT_GATE_ENABLED", "0")
    gate = ChatGate()
    assert gate.enabled is False

    async def scenario():
        ticket = await gate.acquire()
        ticket.release()
        return gate.snapshot()

    assert run_async(scenario())["active"] == 0
