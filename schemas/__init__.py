"""对外契约（Pydantic schema）。

为什么在项目根目录而不是 fastapi_app 里：
    `agent/tools/mcp_server.py` 也要用 ProductRead 来决定工具输出的形状。
    如果 schema 放在 fastapi_app 下，agent 层就要反过来依赖 web 层，
    依赖方向就错了（agent 应该在 web 之下，而不是与之纠缠）。
"""
from schemas.auth import (
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
    UserRead,
)
from schemas.chat import (
    ChatRequest,
    ChatResponse,
    ClearHistoryResponse,
    StatusResponse,
    UploadResponse,
)
from schemas.knowledge import (
    DeleteResponse,
    DocumentRead,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
)
from schemas.product import ProductRead

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ClearHistoryResponse",
    "StatusResponse",
    "UploadResponse",
    "ProductRead",
    "RegisterRequest",
    "TokenPairResponse",
    "RefreshRequest",
    "UserRead",
    "LogoutResponse",
    "KnowledgeBaseCreate",
    "KnowledgeBaseRead",
    "DocumentRead",
    "DeleteResponse",
]
