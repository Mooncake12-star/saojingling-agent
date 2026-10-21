"""用户隔离测试：A 绝对不能看到 B 的数据。

这类测试和普通功能测试的写法不同：它要**主动扮演坏人**。
只测「A 能看到自己的数据」没有意义——那在完全不做隔离的实现里也能通过。
必须测「A 拿着 B 的标识去要数据，拿不到」。

覆盖三处隔离点，任何一处漏掉都会泄露：
    1. SQL 层：repository 的每个查询都必须带 user_id
    2. 内存层：AgentManager 的缓存 key 必须是 (user_id, session_id)
    3. 接口层：路由不接受「别人的 user_id」这个参数
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import db.session as db_session
import fastapi_app.deps as deps_module
from db.base import Base
from db.session import build_engine, session_scope
from fastapi_app.api import api_router
from fastapi_app.api.dependencies import require_agent_manager
from repositories.conversation import ConversationRepository
from tests_helpers import register_and_login


class _WritingAgent:
    """假 Agent：不调大模型，但会像真 Agent 那样把消息写进库。

    这样才测得到「写入时用的 user_id 是请求者的，而不是某个全局默认值」。
    """

    def __init__(self, user_id: int, session_id: str):
        self.user_id = user_id
        self.session_id = session_id

    async def execute_stream(self, query, mcp_manager=None):
        with session_scope() as s:
            ConversationRepository(s).append(self.user_id, self.session_id, "human", query)
        yield "ok"

    def clear_history(self):
        with session_scope() as s:
            ConversationRepository(s).clear(self.user_id, self.session_id)


class _FakeManager:
    mcp_mgr = None
    is_ready = True

    def get_agent(self, user_id: int, session_id=None):
        sid = (session_id or "").strip() or "default"
        return _WritingAgent(user_id, sid)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = build_engine(f"sqlite:///{(tmp_path / 'iso.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", TestingSession)

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_agent_manager] = lambda: _FakeManager()
    with TestClient(app) as c:
        yield c
    engine.dispose()


def chat(client, headers, query, session_id="default"):
    return client.post(
        "/chat", json={"query": query, "session_id": session_id}, headers=headers
    )


# ---------- SQL 层 ----------

def test_same_session_id_is_isolated(client):
    a = register_and_login(client, "a@test.local")
    b = register_and_login(client, "b@test.local")

    assert chat(client, a["headers"], "A 的私密问题").status_code == 200
    assert chat(client, b["headers"], "B 的问题").status_code == 200

    with session_scope() as s:
        repo = ConversationRepository(s)
        a_rows = repo.load_window(a["user_id"], "default")[2]
        b_rows = repo.load_window(b["user_id"], "default")[2]

    assert [r[2] for r in a_rows] == ["A 的私密问题"]
    assert [r[2] for r in b_rows] == ["B 的问题"]


def test_session_list_only_shows_own(client):
    a = register_and_login(client, "a@test.local")
    b = register_and_login(client, "b@test.local")

    chat(client, a["headers"], "x", session_id="only-a")
    chat(client, b["headers"], "y", session_id="only-b")

    a_list = client.get("/history/sessions", headers=a["headers"]).json()
    b_list = client.get("/history/sessions", headers=b["headers"]).json()

    assert [row["session_id"] for row in a_list] == ["only-a"]
    assert [row["session_id"] for row in b_list] == ["only-b"]


def test_clear_does_not_affect_other_user(client):
    a = register_and_login(client, "a@test.local")
    b = register_and_login(client, "b@test.local")

    chat(client, a["headers"], "A 的话", session_id="shared")
    chat(client, b["headers"], "B 的话", session_id="shared")

    assert client.post("/history/clear?session_id=shared", headers=a["headers"]).status_code == 200

    with session_scope() as s:
        repo = ConversationRepository(s)
        assert repo.count_messages(a["user_id"], "shared") == 0
        assert repo.count_messages(b["user_id"], "shared") == 1


# ---------- 内存层 ----------

def test_agent_cache_key_includes_user(monkeypatch):
    """两个用户请求同一个 session_id，必须拿到不同的 Agent 对象。

    如果 key 只用 session_id，B 会直接复用 A 的 ReactAgent——
    它的 conversation_history 里装着 A 的对话。数据一行没漏查，照样泄露。
    """
    monkeypatch.setattr(
        deps_module,
        "ReactAgent",
        lambda session_id, user_id: SimpleNamespace(session_id=session_id, user_id=user_id),
    )
    mgr = deps_module.AgentManager()

    a_agent = mgr.get_agent(1, "default")
    b_agent = mgr.get_agent(2, "default")

    assert a_agent is not b_agent
    assert a_agent.user_id == 1
    assert b_agent.user_id == 2
    # 同一个用户的同一个会话仍然复用
    assert mgr.get_agent(1, "default") is a_agent


# ---------- 接口层 ----------

def test_business_endpoints_all_require_login(client):
    """逐个接口验证：不能靠「记得加依赖」，要靠测试固定住。"""
    for method, path in [
        ("post", "/chat"),
        ("post", "/chat/stream"),
        ("post", "/history/clear"),
        ("get", "/history/sessions"),
    ]:
        resp = getattr(client, method)(path, **({"json": {"query": "hi"}} if method == "post" else {}))
        assert resp.status_code == 401, f"{path} 没有要求登录"


def test_route_signature_has_no_user_id_parameter(client):
    """接口不接受 user_id 参数：身份只能来自 token。

    如果允许 body 里传 user_id，前端（或被篡改的请求）就能指定读谁的数据。
    """
    a = register_and_login(client, "a@test.local")
    b = register_and_login(client, "b@test.local")

    chat(client, a["headers"], "A 的话")
    # 冒充：带着自己的 token，body 里塞别人的 user_id
    resp = client.post(
        "/chat",
        json={"query": "偷看", "session_id": "default", "user_id": b["user_id"]},
        headers=a["headers"],
    )
    assert resp.status_code == 200   # 多余字段被忽略

    with session_scope() as s:
        repo = ConversationRepository(s)
        # 新消息仍然记在 A 名下，没有一条落到 B 名下
        assert repo.count_messages(b["user_id"], "default") == 0
