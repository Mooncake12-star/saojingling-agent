import asyncio
import os
import sys

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

    def get_agent(self, user_id: int, session_id: Optional[str] = None) -> ReactAgent:
        """按「用户 + 会话」取 Agent：每个用户的每个会话一份独立历史。"""
        sid = (session_id or "").strip() or "default"
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

        # 后台预加载 MCP + Reranker 模型，不阻塞服务器启动
        asyncio.create_task(self._init_mcp())

        logger.info("[FastAPI] Agent 服务已启动（MCP 后台加载中）")



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
