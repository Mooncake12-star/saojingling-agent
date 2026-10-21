"""鉴权链路测试：注册 → 登录 → 带 token 访问 → 刷新 → 登出。

为什么用 HTTP 全链路而不是只测函数：
    鉴权的 bug 大多出现在「拼接处」——依赖注入挂错位置、401 写成 403、
    token 类型没校验。只测 security.hash_password 通过，不代表接口是安全的。

用 SessionLocal 打补丁的方式切到临时库：
    db.session.SessionLocal 是模块级变量，get_db 和 session_scope 都在运行时读它，
    patch 掉之后「路由」和「Agent 写库」两条路都指向同一个临时库，
    不需要在每个依赖上分别 override。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import db.session as db_session
from db.base import Base
from db.session import build_engine
from fastapi_app.api import api_router
from fastapi_app.services import security
from models.user import User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = build_engine(f"sqlite:///{(tmp_path / 'auth.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", TestingSession)

    app = FastAPI()
    app.include_router(api_router)
    with TestClient(app) as c:
        yield c
    engine.dispose()


def register(client, email="a@test.local", password="password123"):
    return client.post("/auth/register", json={"email": email, "password": password})


def login(client, email="a@test.local", password="password123"):
    return client.post("/auth/login", data={"username": email, "password": password})


def auth_header(client, email="a@test.local", password="password123") -> dict:
    token = login(client, email, password).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------- 注册 ----------

def test_register_returns_user_without_sensitive_fields(client):
    resp = register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "a@test.local"
    assert body["role"] == "user"
    assert body["is_active"] is True
    # 哈希值和 token 版本绝不能出现在响应里
    assert "password_hash" not in body
    assert "token_version" not in body


def test_register_rejects_duplicate_email_case_insensitively(client):
    assert register(client, "a@test.local").status_code == 201
    # 大小写不同也算同一个邮箱：入库前统一 lower()
    resp = register(client, "A@Test.Local")
    assert resp.status_code == 409


def test_register_rejects_weak_password(client):
    # 少于 8 位被 schema 挡住（Pydantic 的 min_length，422 的 detail 是列表）
    assert register(client, password="1234567").status_code == 422

    # 纯数字被业务规则挡住（services/security.validate_password_strength）
    resp = register(client, password="123456789")
    assert resp.status_code == 422
    assert "纯数字" in resp.json()["detail"]


def test_register_rejects_bad_email(client):
    assert register(client, email="not-an-email").status_code == 422


# ---------- 登录 ----------

def test_login_returns_token_pair(client):
    register(client)
    resp = login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]


def test_login_wrong_password_401(client):
    register(client)
    resp = login(client, password="wrong-password")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_login_unknown_email_401_with_same_message(client):
    """不区分「用户不存在」和「密码错误」，否则可以枚举出哪些邮箱注册过。"""
    register(client)
    resp = login(client, email="ghost@test.local")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "邮箱或密码错误"


def test_login_is_case_insensitive_on_email(client):
    register(client, "Case@Test.Local")
    assert login(client, "case@test.local").status_code == 200


# ---------- token ----------

def test_me_requires_token(client):
    assert client.get("/auth/me").status_code == 401


def test_me_returns_current_user(client):
    register(client)
    resp = client.get("/auth/me", headers=auth_header(client))
    assert resp.status_code == 200
    assert resp.json()["email"] == "a@test.local"


def test_garbage_token_401(client):
    resp = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_refresh_token_cannot_access_business_api(client):
    """refresh 只能换新 token，不能当 access 用。

    不校验 typ 的话，一个长期有效的 refresh 等于永久通行证。
    """
    register(client)
    refresh = login(client).json()["refresh_token"]
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401
    assert "类型" in resp.json()["detail"]


def test_access_token_cannot_refresh(client):
    register(client)
    access = login(client).json()["access_token"]
    assert client.post("/auth/refresh", json={"refresh_token": access}).status_code == 401


def test_refresh_returns_new_pair(client):
    register(client)
    refresh = login(client).json()["refresh_token"]
    resp = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_expired_token_is_rejected(client):
    register(client)
    me = client.get("/auth/me", headers=auth_header(client)).json()

    # 用同一个 secret 手搓一个昨天就过期的 token
    now = datetime.now(timezone.utc) - timedelta(days=1)
    expired = jwt.encode(
        {
            "sub": str(me["id"]),
            "typ": security.TOKEN_ACCESS,
            "ver": 0,
            "iat": int((now - timedelta(minutes=30)).timestamp()),
            "exp": int(now.timestamp()),
            "iss": security.ISSUER,
        },
        security._get_secret(),
        algorithm=security.ALGORITHM,
    )
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert "过期" in resp.json()["detail"]


def test_token_signed_with_other_secret_is_rejected(client):
    """换密钥签的 token 必须无效——这是 secret 唯一的作用。"""
    register(client)
    me = client.get("/auth/me", headers=auth_header(client)).json()
    forged = jwt.encode(
        {
            "sub": str(me["id"]),
            "typ": security.TOKEN_ACCESS,
            "ver": 0,
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            "iss": security.ISSUER,
        },
        "attacker-secret-that-is-long-enough-32",
        algorithm=security.ALGORITHM,
    )
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


# ---------- 登出 / 禁用 ----------

def test_logout_invalidates_existing_token(client):
    """JWT 无状态，靠 users.token_version +1 实现「立刻失效」。"""
    register(client)
    headers = auth_header(client)
    assert client.get("/auth/me", headers=headers).status_code == 200

    assert client.post("/auth/logout", headers=headers).status_code == 200

    resp = client.get("/auth/me", headers=headers)
    assert resp.status_code == 401
    assert "重新登录" in resp.json()["detail"]


def test_new_login_works_after_logout(client):
    """登出只作废旧 token，不影响再次登录。"""
    register(client)
    old = auth_header(client)
    client.post("/auth/logout", headers=old)
    new = auth_header(client)
    assert client.get("/auth/me", headers=new).status_code == 200


def test_disabled_user_cannot_login(client):
    register(client)
    with db_session.session_scope() as s:
        user = s.query(User).filter(User.email == "a@test.local").one()
        user.is_active = False

    assert login(client).status_code == 403


def test_disabled_user_token_stops_working(client):
    register(client)
    headers = auth_header(client)
    with db_session.session_scope() as s:
        s.query(User).filter(User.email == "a@test.local").one().is_active = False

    assert client.get("/auth/me", headers=headers).status_code == 401


# ---------- 密码学原语 ----------

def test_password_hash_is_salted():
    a = security.hash_password("password123")
    b = security.hash_password("password123")
    assert a != b                      # 同一个密码两次结果不同：每次都有随机 salt
    assert "argon2" in a
    assert security.verify_password("password123", a)
    assert not security.verify_password("password124", a)


def test_verify_password_tolerates_broken_hash():
    """库里的哈希串损坏时返回 False，而不是让登录接口 500。"""
    assert not security.verify_password("x", "!disabled")
    assert not security.verify_password("x", "")


# ---------- 邀请码（公网部署才开） ----------

def test_register_without_invite_env_stays_open(client, monkeypatch):
    """没配 ZST_INVITE_CODE 时不设防：本地开发、离线演示不该被邀请码挡住。"""
    monkeypatch.delenv("ZST_INVITE_CODE", raising=False)
    assert register(client).status_code == 201


def test_register_requires_invite_code_when_enabled(client, monkeypatch):
    monkeypatch.setenv("ZST_INVITE_CODE", "zst-2026")
    # 完全不带字段
    assert register(client).status_code == 403
    # 带错的
    resp = client.post(
        "/auth/register",
        json={"email": "a@test.local", "password": "password123", "invite_code": "guess"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "邀请码不正确"


def test_register_succeeds_with_correct_invite_code(client, monkeypatch):
    monkeypatch.setenv("ZST_INVITE_CODE", "zst-2026")
    resp = client.post(
        "/auth/register",
        json={"email": "a@test.local", "password": "password123", "invite_code": "zst-2026"},
    )
    assert resp.status_code == 201
    assert resp.json()["email"] == "a@test.local"


def test_invite_check_runs_before_email_lookup(client, monkeypatch):
    """邀请码不对时，即使邮箱已注册也必须返回 403。

    否则这个接口就成了邮箱枚举器：拿错误邀请码去试，
    409 说明「注册过」、403 说明「没注册过」——邀请码等于白加。
    """
    monkeypatch.delenv("ZST_INVITE_CODE", raising=False)
    assert register(client).status_code == 201   # 先正常注册一个

    monkeypatch.setenv("ZST_INVITE_CODE", "zst-2026")
    resp = client.post(
        "/auth/register",
        json={"email": "a@test.local", "password": "password123", "invite_code": "wrong"},
    )
    assert resp.status_code == 403
    assert "已注册" not in resp.json()["detail"]


def test_invite_code_with_non_ascii_does_not_crash(monkeypatch):
    """中文邀请码不能因为 compare_digest 只吃 ASCII 而抛 TypeError 变成 500。"""
    from fastapi_app.services.auth import InvalidInviteCode, check_invite_code

    monkeypatch.setenv("ZST_INVITE_CODE", "扫净灵内测")
    check_invite_code("扫净灵内测")          # 不抛
    with pytest.raises(InvalidInviteCode):
        check_invite_code("扫净灵公测")


def test_invite_code_ignores_surrounding_spaces(monkeypatch):
    """从聊天软件复制邀请码经常带空格或换行，不该因此被拒。"""
    from fastapi_app.services.auth import check_invite_code

    monkeypatch.setenv("ZST_INVITE_CODE", "  zst-2026  ")
    check_invite_code("zst-2026\n")
