"""请求级上下文：request_id 与当前用户。

为什么用 `ContextVar` 而不是全局变量 / threading.local：
    - 全局变量：并发请求会互相覆盖，日志里出现「A 的请求带 B 的 id」，
      比没有 id 更糟——它会把人带到错误的方向去查。
    - `threading.local`：只按线程隔离。同步接口跑在线程池、异步接口跑在事件循环，
      一个请求会跨线程（`run_in_threadpool` / `asyncio.to_thread`），local 变量会丢。
    - `ContextVar`：按「执行上下文」隔离，asyncio 的每个 Task 自动复制一份，
      进线程池时也会把当前上下文带进去。

**关键细节（踩过的坑）**：`ContextVar.set()` 在子上下文里不会反向传播。
    FastAPI 把 `def`（同步）依赖和接口丢进线程池跑，线程池拿到的是一份
    上下文**副本**。在副本里 `set()` 只改副本，回到请求协程还是旧值——
    现象就是「request_id 有值、user_id 永远是 None」。
    所以这里不存裸值，而是存一个**可变对象**：副本共享同一个对象引用，
    改属性等于改所有上下文都能看到的那一份。这是「跨线程传递可变状态」
    的标准解法，也是 why 这段代码看起来绕的原因。

为什么单独一个模块：
    日志格式化器、中间件、鉴权依赖三处都要读写这几个值。
    放在 logger_handler 里会导致「只为拿 request_id 而 import 日志模块」，
    放在中间件里又会让日志模块反向依赖 web 层。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass

# 上游 nginx / 网关可能已经生成了一个 id，超过这个长度就截断，
# 避免有人拿 10KB 的 header 把日志撑爆（日志注入的一种）
MAX_REQUEST_ID_LEN = 64

# 没有 request_id 时的占位符：日志里永远有字段，格式化时不用到处判空
NO_REQUEST_ID = "-"


@dataclass
class RequestContext:
    """一次请求的可变上下文。字段少、只增不改语义，不做成 dict 是为了有补全。"""

    request_id: str = NO_REQUEST_ID
    user_id: int | None = None


_current: ContextVar[RequestContext | None] = ContextVar("request_ctx", default=None)


def new_request_id() -> str:
    """生成一个短 id。

    16 位十六进制（64 bit）而不是完整的 uuid4：日志里短、肉眼可读、
    一次排查内不可能撞。真要跨服务全局唯一，就在网关生成完整 uuid 传下来。
    """
    return uuid.uuid4().hex[:16]


def bind_request(request_id: str) -> Token:
    """请求入口调用：为本次请求创建一个全新的上下文容器。

    每次请求都新建对象（而不是复用），这样「上一个请求的 user_id 泄漏到
    下一个请求」在结构上就不可能发生。
    """
    return _current.set(RequestContext(request_id=request_id))


def reset_request(token: Token) -> None:
    """用 token 还原，而不是「记旧值再 set 回去」。

    token 还原是幂等的，中间件里抛异常时也能正确弹出这一层；
    手动赋值一旦顺序错乱就会把上一个请求的 id 留在上下文里。
    """
    _current.reset(token)


def get_request_id() -> str:
    ctx = _current.get()
    return ctx.request_id if ctx is not None else NO_REQUEST_ID


def get_user_id() -> int | None:
    ctx = _current.get()
    return ctx.user_id if ctx is not None else None


def set_user_id(value: int | None) -> None:
    """鉴权通过后调用。

    **注意这里改的是对象属性，不是 `ContextVar.set()`**：
    鉴权依赖跑在线程池里，set() 出不了那个线程（见模块开头的说明）。
    属性赋值能穿透，因为各上下文共享同一个 RequestContext 实例。
    """
    ctx = _current.get()
    if ctx is None:
        # 没有中间件的场景（脚本、后台任务、单测直调）：挂一个兜底的容器，
        # 至少本上下文内的日志能带上 uid
        ctx = RequestContext()
        _current.set(ctx)
    ctx.user_id = value
