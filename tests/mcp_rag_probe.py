"""隔离探针：直接对 MCP 子进程发起 initialize + tools/call rag_summarize，
绕开 FastAPI / Agent，用于定位"RAG 调用卡死"到底发生在哪一层。

用法：
    D:\\anaconda3\\envs\\langchain_env\\python.exe tests/mcp_rag_probe.py
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.path.join(ROOT, "agent", "tools", "mcp_server.py")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONUNBUFFERED": "1"},
    )
    log(f"启动子进程: {SERVER}")
    t0 = time.time()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=180)
            log(f"✅ 握手完成 {time.time() - t0:.1f}s")

            tools = await session.list_tools()
            log(f"✅ 工具列表: {[t.name for t in tools.tools]}")

            # 分步测试：先 product_recommend（轻量），再 rag_summarize（重）
            for name, args in [
                ("product_recommend", {"query": "大户型适合什么扫地机器人"}),
                ("rag_summarize",
                 {"query": "扫地机器人轮子卡住故障现象原因及排查处理步骤"}),
            ]:
                t = time.time()
                log(f"→ 调用 {name}")
                try:
                    res = await asyncio.wait_for(
                        session.call_tool(name, args), timeout=240
                    )
                    body = res.content[0].text if res.content else ""
                    log(f"✅ {name} 返回 {time.time() - t:.1f}s，长度 {len(body)}")
                    log(f"   前 120 字: {body[:120]!r}")
                except asyncio.TimeoutError:
                    log(f"❌ {name} 超时 240s 未返回")
                    return

    log(f"全部完成，总耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
