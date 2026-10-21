"""重排模型的进程级访问层：只加载一份、限并发、忙时降级。

为什么要把这块单独抽出来
────────────────────────────────────────
改造前这段逻辑写在 rag_service._get_bge_reranker() 里，形态是「看全局变量，
为 None 就加载」，没有任何互斥。这会同时出两个事故：

1. 重复加载（内存直接翻倍）
   100 并发首次检索时，几十个工作线程同时看到 _RERANKER_INSTANCE is None，
   于是各自执行 CrossEncoder(...)。实测内存 700MB → 1.53GB，机器开始换页，
   之后所有请求表现为「不报错、也不返回」，比直接 500 更难排查。

2. 推理无节制（2 核机器上并发是负收益）
   CrossEncoder.predict 是同步 CPU 密集计算，40 个线程同时跑不会更快，
   只会互相抢 GIL、抢 L3 缓存，单次耗时反而变长；
   而每个线程都占着 FastAPI 的 anyio 工作线程不放，线程池很快被吃干，
   连「查询产品列表」这种不需要模型的接口都开始超时。

所以这一层的三条职责是分明的：
   - 加载：一把锁 + 一个 Event，全进程只加载一次，加载期间不阻塞请求；
   - 推理：一个信号量限制同时推理的线程数，排队超时就直接放弃；
   - 降级：任何一步失败都返回 None，由调用方退回 RRF 融合顺序——
           「少一次精排」远好于「整条链路卡死」。
"""
from __future__ import annotations

import os
import threading
import time

from utils.logger_handler import logger

RERANKER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"

# 状态机取值，同时用于日志与 /readyz
STATE_IDLE = "idle"            # 还没开始加载
STATE_LOADING = "loading"      # 后台线程正在加载，请求应当降级而不是等待
STATE_READY = "ready"          # 可用
STATE_DISABLED = "disabled"    # 依赖缺失或显式关闭
STATE_FAILED = "failed"        # 加载失败


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning(f"[Reranker] 环境变量 {name}={raw!r} 不是整数，使用默认值 {default}")
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning(f"[Reranker] 环境变量 {name}={raw!r} 不是数字，使用默认值 {default}")
        return default


def _local_snapshot_dir(repo_id: str):
    """在 HuggingFace 缓存里找已下载的模型快照目录，找不到返回 None。

    sentence-transformers 5.x 即使传 local_files_only=True，仍会去校验仓库
    元数据（实测 hf-mirror 返回 502 时整个加载直接失败）。直接把本地快照目录
    当「路径」传进去，hf 侧就不会发起任何网络请求——断网、镜像站抖动都不影响。
    """
    hf_home = os.getenv("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    snapshots = os.path.join(hf_home, "hub", "models--" + repo_id.replace("/", "--"), "snapshots")
    if not os.path.isdir(snapshots):
        return None
    candidates = [
        os.path.join(snapshots, name)
        for name in os.listdir(snapshots)
        if os.path.isfile(os.path.join(snapshots, name, "config.json"))
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


class Reranker:
    """进程级重排器。业务侧只用 `available()` / `score()` / `warmup()`。"""

    def __init__(self) -> None:
        # 同一时刻允许几个线程做推理。默认 2：2C4G 机器上再多就是互相拖慢，
        # 而重排本身只占整个响应的一小段（LLM 生成才是大头）。
        self.concurrency = _env_int("ZST_RERANK_CONCURRENCY", 2)
        # 排队等闸门的最长时间。等不到就降级，不把请求挂在锁上——
        # 这是「宁可答案粗一点，也不能让用户等 80 秒」的取舍。
        self.gate_timeout = _env_float("ZST_RERANK_GATE_TIMEOUT", 2.0)
        # 预热后仍然留一小段等待：刚加载完的瞬间，让前几个请求能等到模型可用
        self.ready_timeout = _env_float("ZST_RERANK_READY_TIMEOUT", 0.05)

        self._state = STATE_IDLE
        self._state_lock = threading.Lock()   # 保护状态切换与加载过程
        self._model = None
        self._ready = threading.Event()
        self._gate = threading.BoundedSemaphore(self.concurrency)
        self._gate_timeout_seconds = self.gate_timeout

        # 计数：压测时用日志/接口读出来，判断「降级是不是因为模型不可用」
        self._stats_lock = threading.Lock()
        self.stats = {
            "load_attempts": 0,
            "score_calls": 0,
            "score_skipped_busy": 0,     # 排队超时被降级
            "score_skipped_not_ready": 0,  # 模型还没就绪被降级
            "score_errors": 0,
        }

    # ──────────────────────────────────────────
    # 状态
    # ──────────────────────────────────────────
    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def available(self) -> bool:
        """模型是否可用。不触发加载，也不会阻塞——请求路径上调用。"""
        return self._state == STATE_READY

    def _bump(self, key: str, delta: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + delta

    def snapshot(self) -> dict:
        with self._stats_lock:
            stats = dict(self.stats)
        return {
            "state": self.state,
            "model": RERANKER_MODEL_NAME,
            "concurrency": self.concurrency,
            **stats,
        }

    # ──────────────────────────────────────────
    # 加载
    # ──────────────────────────────────────────
    def _load(self) -> None:
        """真正加载模型。调用方必须已持有 _state_lock，且已把状态置为 LOADING。

        持锁加载是有意的：加载只有一次，把状态机和加载绑在一起，
        其他线程进来看到 LOADING 会立刻返回，不会排队等这把锁。
        """
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            self._state = STATE_DISABLED
            logger.warning("[Reranker] sentence-transformers 未安装，降级到 LLM 重排序")
            return

        self._bump("load_attempts")
        try:
            local_dir = _local_snapshot_dir(RERANKER_MODEL_NAME)
            if local_dir:
                self._model = CrossEncoder(local_dir)
                logger.info(f"[Reranker] 已从本地缓存加载模型: {local_dir}")
            else:
                logger.info("[Reranker] 本地无缓存，开始在线下载")
                self._model = CrossEncoder(RERANKER_MODEL_NAME)
                logger.info(f"[Reranker] 已加载模型: {RERANKER_MODEL_NAME}")
            self._state = STATE_READY
            self._ready.set()
        except Exception as exc:  # noqa: BLE001 —— 加载失败不能让进程起不来
            self._model = None
            self._state = STATE_FAILED
            logger.warning(f"[Reranker] 模型加载失败，降级到 LLM 重排序: {exc}")

    def warmup(self, wait: bool = False, timeout: float | None = None) -> bool:
        """加载模型。已加载 / 正在加载时直接返回，不会重复加载。

        wait=False（默认）时如果别的线程正在加载，立刻返回 False 而不是等——
        启动预热用后台线程，请求路径上不应该因为这个调用堵住。
        """
        with self._state_lock:
            if self._state == STATE_READY:
                return True
            if self._state in (STATE_DISABLED, STATE_FAILED):
                return False
            if self._state == STATE_LOADING:
                # 别人在加载。是否等待由调用方决定
                pass
            else:
                self._state = STATE_LOADING
                self._load()
                return self._state == STATE_READY

        # 走到这里说明是「别人正在加载」
        if not wait:
            return False
        return self._ready.wait(timeout if timeout is not None else 120)

    def warmup_async(self, name: str = "reranker-warmup") -> threading.Thread:
        """后台线程预热：不阻塞 FastAPI 启动。

        为什么不在 lifespan 里同步加载：加载 40~60 秒，容器 start_period 会被
        吃满，编排层以为启动失败直接重启，陷入重启循环。
        """
        def _run() -> None:
            started = time.perf_counter()
            ok = self.warmup()
            cost = time.perf_counter() - started
            logger.info(
                "[Reranker] 后台预热结束",
                extra={"context": {"event": "reranker.warmup", "ok": ok, "cost_s": round(cost, 2),
                                   "state": self.state}},
            )

        thread = threading.Thread(target=_run, name=name, daemon=True)
        thread.start()
        return thread

    # ──────────────────────────────────────────
    # 推理
    # ──────────────────────────────────────────
    def score(self, pairs: list[list[str]]) -> list[float] | None:
        """给 (query, passage) 打分。

        返回 None 的三种情况，调用方都应降级为「保持原顺序」：
            - 模型没就绪（还在加载 / 已禁用 / 加载失败）
            - 等闸门超时（并发已满，排队会拖垮整条链路）
            - 推理抛异常（显存/内存不足等）
        绝不抛异常给上层：重排是「优化项」，不是「必经项」。
        """
        if not pairs:
            return []
        self._bump("score_calls")

        if self._state != STATE_READY or self._model is None:
            # 就绪的瞬间可能刚开始加载，给一个极短的等待窗口；
            # 超过就放弃，不把请求压在 Event 上
            if not self._ready.wait(self.ready_timeout):
                self._bump("score_skipped_not_ready")
                return None
            if self._state != STATE_READY or self._model is None:
                self._bump("score_skipped_not_ready")
                return None

        acquired = self._gate.acquire(timeout=self._gate_timeout_seconds)
        if not acquired:
            self._bump("score_skipped_busy")
            logger.debug(
                "[Reranker] 推理并发已满，本次跳过精排",
                extra={"context": {"event": "reranker.skip", "reason": "busy"}},
            )
            return None
        try:
            started = time.perf_counter()
            scores = self._model.predict(pairs)
            logger.debug(
                "[Reranker] 完成打分",
                extra={"context": {"event": "reranker.score", "pairs": len(pairs),
                                   "cost_s": round(time.perf_counter() - started, 3)}},
            )
            return [float(s) for s in scores]
        except Exception as exc:  # noqa: BLE001
            self._bump("score_errors")
            logger.warning(f"[Reranker] 推理失败，本次跳过精排: {exc}")
            return None
        finally:
            self._gate.release()


# 进程级唯一实例：所有 RagSummarizeService 共用
reranker = Reranker()
