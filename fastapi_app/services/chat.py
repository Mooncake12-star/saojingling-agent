"""对话业务：拼 prompt、驱动 Agent、组织 SSE 帧。

从路由里剥出来的原因很具体：
    改造前 `/chat` 和 `/chat/stream` 各写了一遍「把 image_path 拼进 query」的逻辑。
    两处一旦不同步，同一个问题走同步接口和流式接口会得到不同答案，而且极难发现。
    现在只有 build_query_text 一处决定 prompt 长什么样，两个接口都调它。

P2 之后多了一个 user_id：它不是可选的装饰参数，而是取 Agent 的坐标之一。
删掉它就会拿到别人的会话对象，所以这里没有默认值。
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from agent.react_agent import ReactAgent
from fastapi_app.deps import AgentManager, manager

# 拼给 Agent 的图片提示。抽成常量，改文案时不用在两处同步
IMAGE_HINT = "\n\n[用户已上传图片，文件路径：{path}，如需分析图片请使用 image_analyze 工具]"


def build_query_text(query: str, image_path: str | None = None) -> str:
    """把图片路径追加到问题后面。

    image_path 不存在时静默忽略：路径是前端传的，可能是上一个会话遗留的临时文件，
    为此报 400 对用户体验没有好处，Agent 仍能回答纯文本部分。
    """
    if image_path and os.path.exists(image_path):
        return f"{query}{IMAGE_HINT.format(path=image_path)}"
    return query


async def stream_chunks(
    user_id: int,
    query: str,
    image_path: str | None = None,
    session_id: str | None = None,
    agent_manager: AgentManager | None = None,
) -> AsyncGenerator[str, None]:
    """产出模型返回的文本片段（不带 SSE 协议包装）。"""
    mgr = agent_manager or manager
    agent: ReactAgent = mgr.get_agent(user_id, session_id)
    async for chunk in agent.execute_stream(
        build_query_text(query, image_path), mcp_manager=mgr.mcp_mgr
    ):
        yield chunk


async def stream_sse(
    user_id: int,
    query: str,
    image_path: str | None = None,
    session_id: str | None = None,
    agent_manager: AgentManager | None = None,
) -> AsyncGenerator[bytes, None]:
    """产出完整的 SSE 帧：`data: <chunk>\\n\\n`，结尾补 `[DONE]`。

    为什么要显式发 [DONE]：EventSource 只认服务端关闭连接，
    前端如果不解析结束标记，就只能靠 onerror 判断结束，会把正常结束当成错误。
    """
    async for chunk in stream_chunks(user_id, query, image_path, session_id, agent_manager):
        yield f"data: {chunk}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


async def collect_answer(
    user_id: int,
    query: str,
    image_path: str | None = None,
    session_id: str | None = None,
    agent_manager: AgentManager | None = None,
) -> str:
    """把流拼成完整回答，给非流式接口用。"""
    parts: list[str] = []
    async for chunk in stream_chunks(user_id, query, image_path, session_id, agent_manager):
        parts.append(chunk)
    return "".join(parts)
