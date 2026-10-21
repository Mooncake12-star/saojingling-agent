import asyncio
import os
import sys
import time

from typing import Optional

from agent.react_agent import ReactAgent, ReactMCPContextManager
from rag.local_tool import local_rag_summarize
from rag.vector_store import VectorStoreService
from utils.logger_handler import logger
from db.health import ensure_schema_ready


class AgentManager:
    """管理 Agent 和 MCP 子进程的生命周期"""

    # 最多同时缓存多少个会话的 Agent，超出按最早创建淘汰，防止长期运行内存无限涨
    MAX_SESSIONS = 200

    def __init__(self):
        self.mcp_mgr: Optional[ReactMCPContextManager] = None
        self._mcp_started = False
        self._rag_ready = False
        self._startup_done = False
        # key 必须是 (user_id, session_id)，不能只用 session_id。
        # 只用 session_id 的话，两个用户都请求 "default" 会拿到同一个 ReactAgent 对象，
        # A 的对话历史会直接出现在 B 的上下文里——SQL 一条没漏查，照样越权。
        self._agents: dict[tuple[int, str], ReactAgent] = {}
        # 无状态模式：每个请求新建 Agent。
        #
        # 为什么默认开（ZST_AGENT_STATELESS=1）：
        #   ReactAgent 的历史完全来自数据库（_load_history → load_window 一条 SQL），
        #   内存里的 conversation_history 只是派生缓存。既然如此，缓存它换来的
        #   只是「省一次主键索引查询」，代价却是「多个进程各持一份过期副本」——
        #   一旦 uvicorn 起多 worker 或多副本，同一个会话落到不同进程就会看到
        #   不同的历史，用户刷新页面发现「刚才说的话模型不记得了」。
        #   为了省一次 SQL 引入一类难复现的 bug，不划算。
        #   本地单进程想省这一次查询，把它设成 0 即可。
        self.stateless = os.getenv("ZST_AGENT_STATELESS", "1").strip() not in ("0", "false", "False")

    def get_agent(self, user_id: int, session_id: Optional[str] = None) -> ReactAgent:
        """按「用户 + 会话」取 Agent：每个用户的每个会话一份独立历史。"""
        sid = (session_id or "").strip() or "default"
        if self.stateless:
            # 每次从库里重建窗口：50~200 微秒，换来多 worker 下行为一致
            return ReactAgent(session_id=sid, user_id=user_id)

        key = (user_id, sid)
        agent = self._agents.get(key)
        if agent is None:
            if len(self._agents) >= self.MAX_SESSIONS:
                oldest = next(iter(self._agents))
                self._agents.pop(oldest, None)
                logger.info(f"[FastAPI] 会话数超上限，淘汰最旧会话: user={oldest[0]} session={oldest[1]}")
            agent = ReactAgent(session_id=sid, user_id=user_id)
            self._agents[key] = agent
            logger.info(f"[FastAPI] 新建会话: user={user_id} session={sid}")
        return agent

    async def startup(self):
        logger.info("[FastAPI] 正在启动 Agent 服务...")
        # 表结构由 Alembic 管理：这里只做检查，缺表直接带修复命令报错，
        # 不再像以前那样用 get_connection() 顺手建表（那会掩盖「忘跑迁移」）
        version = ensure_schema_ready()
        logger.info(f"[FastAPI] 数据库迁移版本: {version}")
        # 不再预建「默认会话」：P2 之后每个 Agent 都属于某个用户，
        # 启动时没有用户，也就没有该建的会话。就绪状态由标志位表示。
        self._startup_done = True
        # 把 data/ 下的知识库文件加载到 ChromaDB（快速，不影响启动）
        self._init_knowledge()

        # 后台预加载 MCP + Reranker 模型 + RAG 服务，不阻塞服务器启动
        asyncio.create_task(self._init_mcp())
        asyncio.create_task(self._warmup())

        logger.info(
            "[FastAPI] Agent 服务已启动（MCP / 重排模型后台加载中）",
            extra={"context": {"event": "app.ready", "agent_stateless": self.stateless}},
        )

    async def _warmup(self) -> None:
        """后台预热重排模型与 RAG 服务。

        为什么值得单独做这一步：
            重排模型冷加载 40~60 秒。不预热的话，这段成本由「部署后第一个提问的人」
            承担：他的请求会卡 60 秒，30 秒的 nginx read timeout 先到，用户看到 504，
            但模型其实已经加载好了——第二个人开始就很快。预热把这条曲线拉平，
            也让 /readyz 有真实的状态可报。

        放后台线程而不是 lifespan 里同步 await：
            容器 start_period 只有 60 秒，同步加载会把它吃满，
            编排层判定启动失败开始重启，陷入「重启 → 重新加载 → 又超时」的死循环。
        """
        started = time.perf_counter()
        try:
            # 先加载模型（本轮真正的瓶颈），再建 RAG 服务。
            # 串行而非并行：两者都要吃内存，2C4G 上并行会把峰值顶到限流线以上
            ok = await asyncio.to_thread(self._warmup_blocking)
        except Exception as exc:  # noqa: BLE001 —— 预热失败不能让进程起不来
            ok = False
            logger.warning(f"[FastAPI] 预热异常: {exc}")
        logger.info(
            "[FastAPI] 预热完成",
            extra={"context": {"event": "app.warmup", "ok": ok,
                               "cost_s": round(time.perf_counter() - started, 2)}},
        )

    @staticmethod
    def _warmup_blocking() -> bool:
        from rag.local_tool import warmup as warmup_rag
        from rag.reranker import reranker

        reranker_ok = reranker.warmup()
        # 预热「全局范围」：既验证 chroma 可读，也让第一个匿名/脚本调用不必等建索引。
        # 登录用户是 (user_id, kb_id) 独立 scope，仍会各自建一次（约 1~2 秒）
        rag_ok = warmup_rag(None)
        return bool(reranker_ok and rag_ok)




    async def shutdown(self):
        logger.info("[FastAPI] 正在关闭 Agent 服务...")
        if self.mcp_mgr:
            try:
                await self.mcp_mgr.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"[FastAPI] MCP 关闭异常: {e}")
        logger.info("[FastAPI] Agent 服务已关闭")

    def _init_knowledge(self):
        try:
            vs = VectorStoreService()
            vs.load_document()
            self._rag_ready = True
            logger.info("[FastAPI] 知识库加载完成")
        except Exception as e:
            logger.warning(f"[FastAPI] 知识库加载异常: {e}")
            self._rag_ready = False

    async def _init_mcp(self):
        SERVER_COMMAND = sys.executable
        root_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(root_dir)
        server_dir = os.path.join(project_root, "agent", "tools")
        server_script_name = "mcp_server.py"

        self.mcp_mgr = ReactMCPContextManager(
            command=SERVER_COMMAND, args=[server_script_name], script_dir=server_dir
        )
        # RAG 检索回落到主进程执行：工具定义仍来自 MCP，实现对大模型透明，
        # 但不再让子进程重复加载一份重排模型（省 1.5GB + 40s）。
        self.mcp_mgr.register_local_tool("rag_summarize", local_rag_summarize)

        try:
            await self.mcp_mgr.__aenter__()
            self._mcp_started = True
            logger.info("[FastAPI] MCP 子进程已拉起")
        except Exception as e:
            logger.error(f"[FastAPI] MCP 拉起失败: {e}")
            self._mcp_started = False

    @property
    def is_ready(self) -> bool:
        return self._startup_done and self._mcp_started


manager = AgentManager()
