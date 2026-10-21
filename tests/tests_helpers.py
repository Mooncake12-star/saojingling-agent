"""测试用的小工具（不是 test_*.py，所以 pytest 不会当成用例收集）。"""
from __future__ import annotations

DEFAULT_PASSWORD = "password123"


def register(client, email: str, password: str = DEFAULT_PASSWORD) -> dict:
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def login(client, email: str, password: str = DEFAULT_PASSWORD) -> dict:
    resp = client.post("/auth/login", data={"username": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def register_and_login(client, email: str, password: str = DEFAULT_PASSWORD) -> dict:
    """注册并登录，返回 token 头与 user_id，供需要身份的多用户场景使用。"""
    user = register(client, email, password)
    tokens = login(client, email, password)
    return {
        "user_id": user["id"],
        "headers": {"Authorization": f"Bearer {tokens['access_token']}"},
        "tokens": tokens,
    }
