from pydantic import BaseModel
from typing import Optional


class ChatRequest(BaseModel):
    query: str
    image_path: Optional[str] = None
    # 会话隔离：前端每个会话传一个自己的 id；不传则落到 "default"
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str


class StatusResponse(BaseModel):
    status: str
    mcp_connected: bool
    rag_ready: bool
