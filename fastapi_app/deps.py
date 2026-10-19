import asyncio
import os
import sys
import threading

from typing import Optional

from agent.react_agent import ReactAgent, ReactMCPContextManager
from rag.vector_store import VectorStoreService
from utils.logger_handler import logger
from db.database import get_connection


# ──────────────────────────────────────────────
# 进程内 RAG 单例
# ──────────────────────────────────────────────
# 为什么要放在主进程而不是 MCP 子进程里：
# MCP 子进程要实现 rag_summarize 就得自己 import torch + transformers 并加载
# BGE 重排模型，占 1.5GB 左右；主进程这边已经加载了向量库。8GB 机器上两份
# 叠加会把系统打进重度换页，子进程直接被换出成"假死"。所以模型全系统只留一份，
# 放在主进程，MCP 侧只保留工具定义。
_rag_service = None
_rag_lock = threading.Lock()


def get_rag_service():
    """惰性创建 RAG 服务（首次检索时才加载重排模型，约 40~60 秒）。"""
    global _rag_service
    if _rag_service is not None:
        return _rag_service
    with _rag_lock:
        if _rag_service is None:
            from rag.rag_service import RagSummarizeService
            logger.info("[FastAPI] 首次检索请求，正在初始化 RAG 服务（加载重排模型）...")
            _rag_service = RagSummarizeService()
            logger.info("[FastAPI] RAG 服务初始化完成")
    return _rag_service


def local_rag_summarize(arguments: dict) -> str:
    """rag_summarize 的本地实现（由 Agent 在自己的线程池里调用）。"""
    query = (arguments or {}).get("query", "")
    if not query:
        return "参数错误：缺少 query 字段"
    try:
        return get_rag_service().rag_summarize(query)
    except Exception as e:
        logger.error(f"[FastAPI] 本地 RAG 检索失败: {e}")
        return f"知识库检索失败：{e}"


class AgentManager:
    """管理 Agent 和 MCP 子进程的生命周期"""

    def __init__(self):
        self.agent: Optional[ReactAgent] = None
        self.mcp_mgr: Optional[ReactMCPContextManager] = None
        self._mcp_started = False
        self._rag_ready = False

    async def startup(self):
        logger.info("[FastAPI] 正在启动 Agent 服务...")
        # 初始化 SQLite 数据库（自动建表 + 导入产品数据）
        get_connection()
        logger.info("[FastAPI] SQLite 数据库已初始化")
        self.agent = ReactAgent()
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
        return self.agent is not None and self._mcp_started


manager = AgentManager()
