"""request_id 中间件：一次请求一个 id，贯穿日志与响应头。

为什么写**纯 ASGI** 中间件而不是用 `BaseHTTPMiddleware`：
    `BaseHTTPMiddleware` 会把下游应用放进一个独立的 anyio task 里跑，
    由此带来两个真实问题——
    1. 下游写进 ContextVar 的值（比如 `get_current_user` 里设的 user_id）
       到了中间件这一层读不到，因为两边是不同的上下文副本；
    2. 它对 `StreamingResponse` 有过「响应头已经发出还在等下游」的历史问题，
       而本项目 `/chat/stream` 是 SSE 长连接，最怕这个。
    纯 ASGI 只是包一包 `(scope, receive, send)` 三元组，没有额外的 task，
    上下文是同一份，流式响应也不会被拦截。

为什么 id 可以由上游给：
    生产链路是 nginx → app。让 nginx 生成并传 `X-Request-ID`，
    同一次请求在 nginx access log 和应用日志里就是同一个 id；
    应用在没收到该头时才自己生成，本地直连也能用。
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from starlette.datastructures import MutableHeaders

from utils.log_context import (
    MAX_REQUEST_ID_LEN,
    bind_request,
    new_request_id,
    reset_request,
)
from utils.logger_handler import logger

# 这些路径的访问日志没有价值：探针每 10 秒一次，只会把业务日志冲掉
_QUIET_PATHS = frozenset({"/healthz", "/health", "/favicon.ico"})

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]


def _incoming_request_id(scope: dict[str, Any]) -> str | None:
    """从头里取出上游给的 request id（大小写不敏感）。

    ASGI 的 header 名统一是小写 bytes，所以不用处理大小写变体；
    但值可能带空白或超长，必须洗一遍再进日志和响应头。
    """
    for name, value in scope.get("headers") or []:
        if name == b"x-request-id":
            try:
                cleaned = value.decode("latin-1").strip()
            except Exception:  # noqa: BLE001 - 坏字节不该让请求 500
                return None
            # 超长直接截断：不然一个 10KB 的头就能把日志行撑爆（日志注入）
            return cleaned[:MAX_REQUEST_ID_LEN] or None
    return None


class RequestContextMiddleware:
    """给每个 HTTP 请求绑定 request_id，并在响应头回写。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            # lifespan / websocket 不走这套，原样透传
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or new_request_id()

        # 每个请求新建一个上下文容器：request_id 进来就定下，user_id 先空着，
        # 由鉴权依赖在通过后填。不用「进入时清空」，因为根本不复用
        token = bind_request(request_id)

        start = time.perf_counter()
        status_code = 500  # 下游没发出 response.start 就异常时，按 500 记

        async def send_with_request_id(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                # MutableHeaders 直接改 message["headers"]，
                # 用 []= 而不是 append：上游若已经写过一个同名字段，只保留一个
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # 这里只记日志、不吞异常：异常要继续往上传给 Starlette 的
            # ServerErrorMiddleware，由它决定返回什么（500 或交给 handler）
            logger.exception(
                "请求处理异常",
                extra={"context": {
                    "event": "http.error",
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                }},
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            path = scope.get("path", "")
            level = logger.debug if path in _QUIET_PATHS else logger.info
            level(
                "请求完成",
                extra={"context": {
                    "event": "http.access",
                    "method": scope.get("method"),
                    "path": path,
                    "status": status_code,
                    "duration_ms": duration_ms,
                }},
            )
            # 弹出这一层上下文。放在 finally 里：异常路径也必须还原，
            # 否则 id 会留在线程的事件循环上下文中，串到下一个请求
            reset_request(token)
