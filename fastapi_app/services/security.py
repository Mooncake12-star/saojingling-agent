"""密码哈希与 JWT 签发/校验。

这一层只做「密码学与协议」，不知道数据库、不知道 HTTP：
    输了密码 → 给出哈希 / 判断对不对
    给了用户 → 给出 token / 解出 token 里的用户
谁去查库、失败了返回 401 还是 403，是 services/auth.py 和 api/auth.py 的事。

为什么不用 passlib：
    passlib 已停止维护（最后一次发版在 2020 年），官方文档自己指向 pwdlib。
    argon2 是当前密码哈希的推荐算法（PHC 胜出者），pwdlib 的 recommended() 就是它。

关于 secret：
    默认值只用于本地开发，生产必须用环境变量 ZST_JWT_SECRET 覆盖。
    secret 泄露 = 任何人都能自己签发 token 冒充任何用户，这是整套鉴权唯一的单点。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError
from pwdlib import PasswordHash

from utils.config_handler import security_conf
from utils.logger_handler import logger

_JWT_CONF = security_conf["jwt"]
ALGORITHM: str = _JWT_CONF["algorithm"]
ISSUER: str = _JWT_CONF["issuer"]
ACCESS_MINUTES: int = int(_JWT_CONF["access_token_minutes"])
REFRESH_DAYS: int = int(_JWT_CONF["refresh_token_days"])
MIN_PASSWORD_LENGTH: int = int(security_conf["password"]["min_length"])

TOKEN_ACCESS = "access"
TOKEN_REFRESH = "refresh"

_password_hash = PasswordHash.recommended()


def _get_secret() -> str:
    """环境变量优先。用默认值时会打一条警告，避免「线上忘了设置」这种情况静默发生。"""
    secret = os.getenv("ZST_JWT_SECRET", "").strip()
    if secret:
        return secret
    logger.warning(
        "[Auth] 未设置 ZST_JWT_SECRET，正在使用开发默认密钥。生产环境必须覆盖！"
    )
    return str(_JWT_CONF["secret"])


# ---------- 密码 ----------

def hash_password(raw: str) -> str:
    return _password_hash.hash(raw)


def verify_password(raw: str, stored_hash: str) -> bool:
    """校验密码。

    哈希串损坏时返回 False 而不是抛异常：
    库里的脏数据不应该让登录接口 500——那是「这个密码不对」的另一种说法。
    注意 pwdlib 抛的 UnknownHashError 不是 ValueError 的子类，
    只捕获 ValueError/TypeError 会漏（踩过一次）。
    """
    try:
        return _password_hash.verify(raw, stored_hash)
    except Exception:  # noqa: BLE001 - 任何解析失败都等价于「验证不通过」
        return False


def validate_password_strength(raw: str) -> str | None:
    """返回错误信息，通过则返回 None。"""
    if len(raw or "") < MIN_PASSWORD_LENGTH:
        return f"密码至少 {MIN_PASSWORD_LENGTH} 位"
    if raw.isdigit():
        return "密码不能是纯数字"
    return None


# ---------- JWT ----------

class TokenError(Exception):
    """token 无效、过期或类型不对。"""


@dataclass(frozen=True)
class TokenPayload:
    user_id: int
    token_version: int
    token_type: str
    expires_at: datetime


def create_token(user_id: int, token_version: int, token_type: str) -> str:
    """签发 token。

    payload 里放 token_version 是为了支持「踢下线」：
    校验时把它和库里的 users.token_version 比对，不一致就作废。
    没有它，JWT 一旦签发就只能等它自己过期。
    """
    if token_type == TOKEN_ACCESS:
        expires = timedelta(minutes=ACCESS_MINUTES)
    elif token_type == TOKEN_REFRESH:
        expires = timedelta(days=REFRESH_DAYS)
    else:
        raise ValueError(f"未知的 token 类型: {token_type}")

    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "typ": token_type,
        "ver": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + expires).timestamp()),
        "iss": ISSUER,
    }
    return jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)


def decode_token(token: str, expected_type: str) -> TokenPayload:
    """解析并校验 token，失败一律抛 TokenError。

    必须显式指定 algorithms：不指定时老版本 PyJWT 会接受 token 头部声明的任意算法，
    其中包含 `none`（不签名）——那是历史上有名的绕过漏洞。
    """
    try:
        raw = jwt.decode(
            token,
            _get_secret(),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "sub", "typ", "iss"]},
        )
    except ExpiredSignatureError as exc:
        raise TokenError("token 已过期") from exc
    except InvalidTokenError as exc:
        raise TokenError("token 无效") from exc

    if raw.get("typ") != expected_type:
        # 用 refresh token 访问业务接口是常见误用，单独给一条明确信息
        raise TokenError(f"token 类型不匹配，需要 {expected_type}")

    try:
        return TokenPayload(
            user_id=int(raw["sub"]),
            token_version=int(raw.get("ver", 0)),
            token_type=raw["typ"],
            expires_at=datetime.fromtimestamp(raw["exp"], tz=timezone.utc),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("token 内容不完整") from exc
