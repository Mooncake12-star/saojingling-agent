"""定位脚本：单独执行 MCP 子进程里"首次检索时的 RAG 初始化"，逐步打点。

用法：
    D:\\anaconda3\\envs\\langchain_env\\python.exe tests/rag_init_probe.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_T0 = time.time()


def mark(msg: str) -> None:
    print(f"[{time.time() - _T0:7.1f}s] {msg}", flush=True)


mark("start")

mark("import rag.rag_service 开始")
from rag.rag_service import RagSummarizeService  # noqa: E402

mark("import rag.rag_service 完成")

mark("实例化 RagSummarizeService 开始")
svc = RagSummarizeService()
mark("实例化 RagSummarizeService 完成")

mark("rag_summarize 调用开始")
out = svc.rag_summarize("扫地机器人轮子卡住故障现象原因及排查处理步骤")
mark(f"rag_summarize 返回，长度 {len(out)}")
print(out[:300], flush=True)
