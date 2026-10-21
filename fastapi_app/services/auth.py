"""注册 / 登录 / 刷新 / 登出 的业务逻辑。

这一层不抛 HTTPException，只抛下面这些领域异常；
「翻译成 400 / 401 / 409」由 api/auth.py 完成。
好处是：以后写 CLI 或定时任务要复用登录逻辑时，不会被迫引入 FastAPI。
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fastapi_app.services import security
from models.user import User
from repositories.user import UserRepository
from utils.logger_handler import logger

# 环境变量名单独抽出来：部署文档、compose、测试都引它，避免各处手打字符串对不上
INVITE_ENV = "ZST_INVITE_CODE"


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


class InvalidInviteCode(AuthError):
    """邀请码不对，或服务端要求邀请码但请求没带。"""


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


def required_invite_code() -> str | None:
    """服务端要求的邀请码；未配置时返回 None。

    为什么用环境变量而不是配置文件：
        邀请码是「部署时才决定」的东西，写进 yml 就会跟着代码进仓库，
        等于把进门的钥匙挂在门上。
    """
    code = os.getenv(INVITE_ENV, "").strip()
    return code or None


def check_invite_code(provided: str | None) -> None:
    """校验邀请码。服务端没配 ZST_INVITE_CODE 时直接放行（本地开发不受影响）。

    用 hmac.compare_digest 而不是 ==：
        字符串比较在第一个不同字符就返回，耗时随「有几个字符是对的」变化。
        这个差异能被人用大量请求统计出来，逐个字符猜出邀请码。
        compare_digest 耗时只与长度有关。
    转 bytes 再比：compare_digest 对 str 只接受 ASCII，
        邀请码里出现中文会直接抛 TypeError，那就变成 500 了。
    """
    expected = required_invite_code()
    if expected is None:
        return
    if not provided or not hmac.compare_digest(
        provided.strip().encode("utf-8"), expected.encode("utf-8")
    ):
        raise InvalidInviteCode("邀请码不正确")


def register(session: Session, email: str, password: str, invite_code: str | None = None) -> User:
    # 邀请码放在最前面校验：不能让人用错误的邀请码去试探「这个邮箱存不存在」，
    # 否则加了邀请码等于没加，注册接口照样是个邮箱枚举器。
    check_invite_code(invite_code)

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
