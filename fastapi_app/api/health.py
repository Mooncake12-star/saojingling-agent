"""运维接口：根路径跳转与健康检查。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from fastapi_app.deps import manager
from schemas import StatusResponse

router = APIRouter(tags=["运维"])


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """浏览器直接访问根路径时跳到交互式文档。"""
    return RedirectResponse(url="/docs")


@router.get("/health", response_model=StatusResponse, summary="健康检查")
async def health_check() -> StatusResponse:
    """给探针 / 负载均衡用的存活检查。

    这里刻意不用 require_agent_manager：那个依赖在未就绪时抛 503，
    而健康检查的职责恰恰是把「未就绪」这个事实**报告**出来。
    真返回 503 的话，探针只能看到「挂了」，看不到「MCP 没起来还是 RAG 没起来」。
    """
    return StatusResponse(
        status="ok" if manager.is_ready else "degraded",
        mcp_connected=manager._mcp_started,
        rag_ready=manager._rag_ready,
    )
