"""对话接口：流式、非流式、清空历史、会话列表。

三个对话接口共用 services/chat.py 里的同一套逻辑，
路由层只剩「读参数 → 调服务 → 包装成 HTTP 响应」。

P2 之后每个接口都依赖 CurrentUser：
    没有它，session_id 就是一句空话——任何人都能猜一个 id 读别人的对话。
    user.id 一路传到 AgentManager，成为取 Agent 的坐标之一。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from db.session import get_db
from fastapi_app.api.dependencies import CurrentUser, require_agent_manager
from fastapi_app.deps import AgentManager
from fastapi_app.services import chat as chat_service
from repositories.conversation import ConversationRepository
from schemas import ChatRequest, ChatResponse, ClearHistoryResponse

router = APIRouter(tags=["对话"])

# SSE 必须加的三个响应头，少一个都会「看起来能用但流不起来」
SSE_HEADERS = {
    "Cache-Control": "no-cache",   # 禁止浏览器/代理缓存，流式数据每次都要最新的
    "Connection": "keep-alive",    # 保持 TCP 长连接
    "X-Accel-Buffering": "no",     # 禁用 Nginx 缓冲，否则 Nginx 攒满一块才转发
}


@router.post("/chat/stream", summary="流式对话（SSE）")
async def chat_stream(
    body: ChatRequest,
    user: CurrentUser,
    mgr: AgentManager = Depends(require_agent_manager),
) -> StreamingResponse:
    return StreamingResponse(
        chat_service.stream_sse(user.id, body.query, body.image_path, body.session_id, mgr),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/chat", response_model=ChatResponse, summary="一次性返回完整回答")
async def chat_sync(
    body: ChatRequest,
    user: CurrentUser,
    mgr: AgentManager = Depends(require_agent_manager),
) -> ChatResponse:
    answer = await chat_service.collect_answer(
        user.id, body.query, body.image_path, body.session_id, mgr
    )
    return ChatResponse(answer=answer)


@router.post("/history/clear", response_model=ClearHistoryResponse, summary="清空会话历史")
async def clear_history(
    user: CurrentUser,
    session_id: str = "default",
    mgr: AgentManager = Depends(require_agent_manager),
) -> ClearHistoryResponse:
    # 内存与库一起清：只清库的话，当前进程里的窗口还留着旧消息，
    # 用户会发现「清空之后 Agent 还记得刚才说的话」。
    mgr.get_agent(user.id, session_id).clear_history()
    return ClearHistoryResponse(message="对话历史已清空", session_id=session_id)


@router.get("/history/sessions", summary="当前用户的会话列表")
def list_sessions(user: CurrentUser, db: Session = Depends(get_db)) -> list[dict]:
    """只返回自己的会话。

    这个方法里没有「可以传别人 user_id」的参数——隔离靠的是接口形状，
    而不是调用时记得加条件。少一个参数，就少一类越权可能。
    """
    rows = ConversationRepository(db).list_sessions(user.id)
    return [{"session_id": sid, "message_count": count} for sid, count in rows]
