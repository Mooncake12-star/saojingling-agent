"""应用装配。

本文件只做四件事，业务代码一行都不写：
    1. lifespan：启动/关闭时的资源管理
    2. 中间件：CORS
    3. 路由：include_router（具体接口在 api/ 下）
    4. 应用元信息

改动前这个文件 147 行，把 6 个接口、SSE 拼装、图片 prompt 拼接、文件落盘全混在一起；
现在剩 50 行左右，接口逻辑按职责分到了 api/ 和 services/。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import asyncio
from pathlib import Path

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from utils.path_tool import ensure_project_root_in_syspath

# 必须在导入本项目其他模块之前执行：保证从任意工作目录启动都能 import 到根目录下的包
ensure_project_root_in_syspath()

from fastapi_app.api import api_router  # noqa: E402
from fastapi_app.deps import manager  # noqa: E402
from fastapi_app.middleware import RequestContextMiddleware  # noqa: E402
from fastapi_app.services import auth as auth_service  # noqa: E402
from fastapi_app.services import cache as cache_service  # noqa: E402
from utils.logger_handler import logger, quiet_noisy_loggers  # noqa: E402

# 启动命令：uvicorn fastapi_app.main:app --host 0.0.0.0 --port 8000
# 交互式文档：http://127.0.0.1:8000/docs


@asynccontextmanager
async def lifespan(app: FastAPI):
    quiet_noisy_loggers()
    # 缓存初始化放在启动阶段：真正的连接错误会在这里打一条警告，
    # 而不是在第一个用户请求里才抛出来
    cache_service.configure_from_env()
    # 公网部署后最容易忘的事：以为邀请码开着，其实环境变量没传进容器。
    # 启动时打一条，部署验收时看一眼日志就能确认
    invite_state = "on" if auth_service.required_invite_code() else "off"
    if invite_state == "off":
        logger.warning(
            "邀请码校验未开启，任何人都能注册",
            extra={"context": {"event": "auth.invite", "state": "off"}},
        )
    logger.info(
        "服务启动",
        extra={
            "context": {
                "event": "app.startup",
                "cache": cache_service.get_cache().backend(),
                "invite_code": invite_state,
            }
        },
    )
    await manager.startup()
    try:
        yield
    finally:
        # 用 finally：启动后如果异常退出，MCP 子进程也必须被回收，
        # 否则下次启动会留下僵尸子进程占着管道
        await manager.shutdown()
        logger.info("服务关闭", extra={"context": {"event": "app.shutdown"}})


app = FastAPI(
    title="扫净灵 AI Agent",
    description="基于 ReAct Agent + RAG + MCP 的智能助手后端服务",
    version="2.1.0",
    lifespan=lifespan,
    # 默认的 /docs 和 /redoc 从 cdn.jsdelivr.net 拉 swagger-ui 的 css/js，
    # 这个域名在部分网络下取不到，页面就退化成一堆没样式的裸 HTML 元素。
    # 先关掉默认实现，下面用本地资源重新注册
    docs_url=None,
    redoc_url=None,
)

# 静态资源自托管：swagger-ui-dist 5.34.0 的三个文件放在 fastapi_app/static/swagger/，
# 不依赖外网也能打开 /docs
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/docs", include_in_schema=False)
async def swagger_ui() -> HTMLResponse:
    """交互式文档页（离线版）。"""
    return get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} - Swagger UI",
        swagger_js_url="/static/swagger/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger/swagger-ui.css",
        swagger_favicon_url="/static/swagger/favicon.png",
    )

# 前后端分离后这部分有用；生产环境要把 allow_origins 收窄到具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # 允许哪些域名访问，["*"]=全部放行
    allow_credentials=True,    # 是否允许前端带 Cookie/Token 等凭证
    allow_methods=["*"],       # 允许的 HTTP 方法
    allow_headers=["*"],       # 允许的自定义请求头
    # 跨域时浏览器默认只让 JS 读 6 个「安全头」，其余的要显式放行。
    # 不给这一行，前端就拿不到 X-Request-ID，用户报障时你只能靠时间点猜
    expose_headers=["X-Request-ID"],
)

# 放在 CORS 之后 add：后加的在外层。这样 500 响应也会带上 X-Request-ID，
# 否则出错的请求恰恰拿不到 id——那是唯一真正需要它的场景
app.add_middleware(RequestContextMiddleware)

app.include_router(api_router)


@app.get("/healthz", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """最轻量的存活探针：不查数据库、不碰 MCP，只证明进程还在。

    和 /health 的区别：这个用来判断「要不要重启进程」，
    /health 用来判断「能不能接流量」。
    """
    return {"status": "alive"}


def _db_ping() -> bool:
    from sqlalchemy import text

    from db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 —— 探针不该因为数据库抖动抛 500
        return False


@app.get("/readyz", include_in_schema=False)
async def readiness(response: Response) -> dict:
    """就绪探针：决定「这个副本要不要进负载均衡」。

    和 /healthz 的分工必须分清，否则会出两类事故：
        用 /healthz 做就绪 → 进程活着但数据库断了，流量照样打进来，全部 500；
        用 /readyz 做存活 → 数据库抖一下，所有副本被判死，被一起重启。

    判定条件（任一不满足返回 503）：
        _startup_done  迁移检查已过
        _mcp_started   MCP 工具集已就绪（没有它对话不可用）
        db_ok          数据库可读

    重排模型**不计入**就绪：它不可用时检索降级为 RRF 顺序，功能仍可用。
    把它算成就绪条件，等于让一个可选优化项决定整个副本能不能接流量。
    """
    from fastapi_app.concurrency import gate
    from rag.local_tool import MAX_SCOPES
    from rag.reranker import reranker

    db_ok = await asyncio.to_thread(_db_ping)
    ready = bool(manager._startup_done and manager._mcp_started and db_ok)  # noqa: SLF001

    payload = {
        "ready": ready,
        "db": "ok" if db_ok else "down",
        "mcp": "ok" if manager._mcp_started else "starting",  # noqa: SLF001
        "reranker": reranker.snapshot(),
        "chat_gate": gate.snapshot(),
        "agent_stateless": manager.stateless,
        "rag_max_scopes": MAX_SCOPES,
        "cache": cache_service.get_cache().backend(),
    }
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload
