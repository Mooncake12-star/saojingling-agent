"""注册 / 登录 / 刷新 / 登出 的业务逻辑。

这一层不抛 HTTPException，只抛下面这些领域异常；
「翻译成 400 / 401 / 409」由 api/auth.py 完成。
好处是：以后写 CLI 或定时任务要复用登录逻辑时，不会被迫引入 FastAPI。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from fastapi_app.services import security
from models.user import User
from repositories.user import UserRepository


class AuthError(Exception):
    """鉴权业务异常基类。"""


class EmailTaken(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


class InactiveUser(AuthError):
    pass


class WeakPassword(AuthError):
    pass


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

    def as_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
        }


def _issue(user: User) -> TokenPair:
    return TokenPair(
        access_token=security.create_token(user.id, user.token_version, security.TOKEN_ACCESS),
        refresh_token=security.create_token(user.id, user.token_version, security.TOKEN_REFRESH),
    )


def register(session: Session, email: str, password: str) -> User:
    problem = security.validate_password_strength(password)
    if problem:
        raise WeakPassword(problem)

    repo = UserRepository(session)
    if repo.get_by_email(email):
        # 这里确实泄露了「该邮箱已注册」。注册接口无法避免这一点
        # （不告诉用户就没法注册），所以真正要做的是限流，而不是模糊错误信息。
        raise EmailTaken("该邮箱已注册")

    return repo.create(email, security.hash_password(password))


def login(session: Session, email: str, password: str) -> TokenPair:
    repo = UserRepository(session)
    user = repo.get_by_email(email)

    # 用户不存在时也走一次哈希校验：否则「响应快 = 用户不存在」，
    # 攻击者能靠响应时间枚举出哪些邮箱注册过。
    stored = user.password_hash if user else security.hash_password("dummy-password")
    ok = security.verify_password(password, stored)

    if not user or not ok:
        raise InvalidCredentials("邮箱或密码错误")
    if not user.is_active:
        raise InactiveUser("账号已被禁用")
    return _issue(user)


def refresh(session: Session, refresh_token: str) -> TokenPair:
    payload = _decode_or_401(refresh_token, security.TOKEN_REFRESH)
    user = UserRepository(session).get(payload.user_id)
    if not user or not user.is_active:
        raise InvalidCredentials("用户不存在或已禁用")
    if payload.token_version != user.token_version:
        raise InvalidCredentials("登录状态已失效，请重新登录")
    # 轮换：每次刷新都发一对新的。旧的 refresh 仍然可用（无状态 JWT 无法主动作废），
    # 但配合较短的 refresh 有效期，泄露窗口是可控的。
    return _issue(user)


def logout_all(session: Session, user: User) -> User:
    """把 token_version +1，该用户所有已签发的 token 立即失效。

    这是 JWT 下最省事的「踢下线 / 改密码强制重登」方案：
    不需要存黑名单，代价是全校失效（不能只踢某一个设备）。
    """
    UserRepository(session).bump_token_version(user.id)
    session.refresh(user)
    return user


def _decode_or_401(token: str, expected_type: str):
    try:
        return security.decode_token(token, expected_type)
    except security.TokenError as exc:
        raise InvalidCredentials(str(exc)) from exc
