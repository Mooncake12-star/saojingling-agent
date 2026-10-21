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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from utils.path_tool import ensure_project_root_in_syspath

# 必须在导入本项目其他模块之前执行：保证从任意工作目录启动都能 import 到根目录下的包
ensure_project_root_in_syspath()

from fastapi_app.api import api_router  # noqa: E402
from fastapi_app.deps import manager  # noqa: E402
from utils.logger_handler import logger  # noqa: E402

# 启动命令：uvicorn fastapi_app.main:app --host 0.0.0.0 --port 8000
# 交互式文档：http://127.0.0.1:8000/docs


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.startup()
    try:
        yield
    finally:
        # 用 finally：启动后如果异常退出，MCP 子进程也必须被回收，
        # 否则下次启动会留下僵尸子进程占着管道
        await manager.shutdown()


app = FastAPI(
    title="扫净灵 AI Agent",
    description="基于 ReAct Agent + RAG + MCP 的智能助手后端服务",
    version="2.1.0",
    lifespan=lifespan,
)

# 前后端分离后这部分有用；生产环境要把 allow_origins 收窄到具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # 允许哪些域名访问，["*"]=全部放行
    allow_credentials=True,    # 是否允许前端带 Cookie/Token 等凭证
    allow_methods=["*"],       # 允许的 HTTP 方法
    allow_headers=["*"],       # 允许的自定义请求头
)

app.include_router(api_router)


@app.get("/healthz", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """最轻量的存活探针：不查数据库、不碰 MCP，只证明进程还在。

    和 /health 的区别：这个用来判断「要不要重启进程」，
    /health 用来判断「能不能接流量」。
    """
    return {"status": "alive"}
