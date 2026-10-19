"""进程内 RAG 单例 + rag_summarize 的"本地优先"实现。

为什么要有这个模块：
MCP 子进程要实现 rag_summarize 就得自己 import torch + transformers 并加载
BGE 重排模型（约 1.5GB 常驻）。主进程这边已经加载了向量库，8GB 内存的机器
上两份叠加会把系统打进重度换页，子进程直接被换出成"假死"（表现为调用永久
卡死、无任何报错）。

所以模型全系统只保留一份，放在主进程；MCP 侧只保留工具定义。
FastAPI（fastapi_app/deps.py）和 Streamlit（app.py）两个入口都要注册。
"""
import threading

from utils.logger_handler import logger

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
            logger.info("[LocalTool] 首次检索请求，正在初始化 RAG 服务（加载重排模型）...")
            _rag_service = RagSummarizeService()
            logger.info("[LocalTool] RAG 服务初始化完成")
    return _rag_service


def local_rag_summarize(arguments: dict) -> str:
    """rag_summarize 的本地实现（由 Agent 在自己的线程池里调用）。"""
    query = (arguments or {}).get("query", "")
    if not query:
        return "参数错误：缺少 query 字段"
    try:
        return get_rag_service().rag_summarize(query)
    except Exception as e:
        logger.error(f"[LocalTool] 本地 RAG 检索失败: {e}")
        return f"知识库检索失败：{e}"
