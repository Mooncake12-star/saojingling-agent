"""对话接口：流式、非流式、清空历史、会话列表。

三个对话接口共用 services/chat.py 里的同一套逻辑，
路由层只剩「读参数 → 调服务 → 包装成 HTTP 响应」。

P2 之后每个接口都依赖 CurrentUser：
    没有它，session_id 就是一句空话——任何人都能猜一个 id 读别人的对话。
    user.id 一路传到 AgentManager，成为取 Agent 的坐标之一。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from db.session import get_db
from fastapi_app.api.dependencies import CurrentUser, require_agent_manager
from fastapi_app.concurrency import ChatBusy, gate
from fastapi_app.deps import AgentManager
from fastapi_app.services import chat as chat_service
from fastapi_app.services import knowledge as knowledge_service
from repositories.conversation import ConversationRepository
from schemas import ChatRequest, ChatResponse, ClearHistoryResponse

router = APIRouter(tags=["对话"])

# SSE 必须加的三个响应头，少一个都会「看起来能用但流不起来」
SSE_HEADERS = {
    "Cache-Control": "no-cache",   # 禁止浏览器/代理缓存，流式数据每次都要最新的
    "Connection": "keep-alive",    # 保持 TCP 长连接
    "X-Accel-Buffering": "no",     # 禁用 Nginx 缓冲，否则 Nginx 攒满一块才转发
}


def _busy(exc: ChatBusy) -> HTTPException:
    """把闸门的「排队超时」翻译成 503。

    必须是 503 + Retry-After，而不是 429：
        503 表示「服务暂时不可用」，客户端和网关会按 Retry-After 退避重试；
        429 语义是「你被限流了」，会被理解成业务配额问题。
        这里的原因是本实例过载，换个实例就能服务——语义上是 503。
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(exc),
        headers={"Retry-After": str(exc.retry_after)},
    )


async def _gated(agen: AsyncGenerator[bytes, None], ticket) -> AsyncGenerator[bytes, None]:
    """在流的外面套一层 finally，保证连接断开时也会归位。

    StreamingResponse 被客户端提前掐断时，生成器会被 close()，
    finally 依然执行——这是 SSE 场景下唯一能可靠回收资源的位置。
    """
    try:
        async for chunk in agen:
            yield chunk
    finally:
        ticket.release()


def _ensure_kb_owned(db: Session, user_id: int, knowledge_base_id: int | None) -> None:
    """在流开始**之前**校验知识库归属。

    必须在返回 StreamingResponse 之前做：一旦状态码发出去（200），
    再发现「这个库不是你的」就只能中途断流，前端拿到的是半截回答。
    先校验，才有机会回一个干净的 404。
    """
    if knowledge_base_id is None:
        return
    try:
        knowledge_service.get_knowledge_base(db, user_id, knowledge_base_id)
    except knowledge_service.KnowledgeBaseNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.post("/chat/stream", summary="流式对话（SSE）")
async def chat_stream(
    body: ChatRequest,
    user: CurrentUser,
    mgr: AgentManager = Depends(require_agent_manager),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    _ensure_kb_owned(db, user.id, body.knowledge_base_id)
    # 先占位再返回响应：占不到就还来得及回 503。
    # 反过来（先返回 200 再在生成器里等位）就没有干净的失败路径了。
    try:
        ticket = await gate.acquire()
    except ChatBusy as exc:
        raise _busy(exc) from exc

    stream = chat_service.stream_sse(
        user.id,
        body.query,
        body.image_path,
        body.session_id,
        mgr,
        body.knowledge_base_id,
    )
    return StreamingResponse(
        _gated(stream, ticket),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
        # 双保险：流正常走完时 _gated 的 finally 已经释放过（release 幂等），
        # 这条兜住「生成器压根没被迭代」的情况
        background=BackgroundTask(ticket.release),
    )


@router.post("/chat", response_model=ChatResponse, summary="一次性返回完整回答")
async def chat_sync(
    body: ChatRequest,
    user: CurrentUser,
    mgr: AgentManager = Depends(require_agent_manager),
    db: Session = Depends(get_db),
) -> ChatResponse:
    _ensure_kb_owned(db, user.id, body.knowledge_base_id)
    try:
        ticket = await gate.acquire()
    except ChatBusy as exc:
        raise _busy(exc) from exc

    try:
        answer = await chat_service.collect_answer(
            user.id, body.query, body.image_path, body.session_id, mgr, body.knowledge_base_id
        )
    finally:
        ticket.release()
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
