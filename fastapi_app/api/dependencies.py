"""路由共用的依赖。

三层职责：
    require_agent_manager → 服务是否就绪（503）
    get_current_user      → 这是谁（401）
    get_current_admin     → 他能不能做这件事（403）

401 和 403 的区别不是修辞：
    401 = 我不知道你是谁（没带 token / token 过期），前端应该跳登录页
    403 = 我知道你是谁，但你没权限，前端应该提示「无权限」，不要跳登录页
把 403 写成 401 会导致前端把有权限的用户反复踢去登录。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from db.session import get_db
from fastapi_app.deps import AgentManager, manager
from fastapi_app.services import security
from models.user import User
from repositories.user import UserRepository

# tokenUrl 指向登录接口：/docs 右上角的 Authorize 按钮就靠它
# auto_error=False：缺 token 时不让 FastAPI 自己抛英文错误，统一走下面的中文 401
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def _unauthorized(detail: str = "未登录或登录状态已失效") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        # 这个头是 OAuth2 规范要求的，缺了它某些客户端不会重试登录
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """从 Bearer token 解析出当前用户。

    每次请求都查一次库，换来的是「能立刻作废」：
    JWT 本身无状态，但用户被禁用、token_version 被 +1 时，
    只有查库才能当场发现。这个查询走主键，代价可以忽略。
    """
    if not token:
        raise _unauthorized()

    try:
        payload = security.decode_token(token, security.TOKEN_ACCESS)
    except security.TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    user = UserRepository(db).get(payload.user_id)
    if user is None:
        # token 本身合法，但用户已被删除
        raise _unauthorized("用户不存在")
    if not user.is_active:
        raise _unauthorized("账号已被禁用")
    if payload.token_version != user.token_version:
        # 改密码 / 主动登出后，旧 token 会走到这里
        raise _unauthorized("已在其他位置退出登录，请重新登录")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]


def require_agent_manager() -> AgentManager:
    """Agent 或 MCP 没准备好时直接 503。

    用 503 Service Unavailable 而不是 500：前者语义是「暂时不可用，稍后重试」，
    能进客户端的重试逻辑；500 会被当成代码 bug。
    """
    if not manager.is_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent 服务未就绪",
        )
    return manager
