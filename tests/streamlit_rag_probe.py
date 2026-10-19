"""验证 Streamlit 入口（app.py）的本地优先逻辑：
完全复刻 app.py 里 _init_mcp 的建法，然后调 rag_summarize，
看它是否走本地（主进程）而不是 MCP 子进程。
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.react_agent import ReactMCPContextManager
from rag.local_tool import local_rag_summarize


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def main():
    server_dir = os.path.join(ROOT, "agent", "tools")
    mgr = ReactMCPContextManager(
        command=sys.executable,
        args=["mcp_server.py"],
        script_dir=server_dir,
    )
    # 与 app.py 相同：注册本地实现
    mgr.register_local_tool("rag_summarize", local_rag_summarize)

    t0 = time.time()
    await asyncio.wait_for(mgr.__aenter__(), timeout=120)
    log(f"MCP 就绪 {time.time() - t0:.1f}s，工具数 {len(mgr.llm_tools_schema)}")

    t = time.time()
    out = await asyncio.wait_for(
        mgr.call_mcp_tool("rag_summarize", {"query": "扫地机器人轮子卡住怎么排查"}),
        timeout=240,
    )
    log(f"rag_summarize 返回 {time.time() - t:.1f}s，长度 {len(out)}")
    log(f"前 100 字: {out[:100]!r}")

    await mgr.__aexit__(None, None, None)
    log("验证完成")


asyncio.run(main())
