"""检索范围（多租户隔离）测试。

要证明三件事：
    1. 范围表达式写对：指定知识库 → user_id + kb_id 双条件；不指定 → 自己的 + 公共资料
    2. 请求入口真的把范围设进去了：Agent 调到工具时，ContextVar 里是当前用户
    3. 别人的知识库在流开始前就被挡掉（404），而不是流到一半才断

特别注意第 2 条的测法：不是断言「函数被调用过」，而是让假 Agent 在
execute_stream 里读一次 current_scope()。这样才验证到
「异步生成器 + 线程池 + ContextVar」这条真实链路上的传递，
而不是验证一个我自己 mock 出来的调用记录。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import db.session as db_session
import rag.local_tool as local_tool
from db.base import Base
from db.session import build_engine
from fastapi_app.api import api_router
from fastapi_app.api.dependencies import require_agent_manager
from rag.scope import RagScope, current_scope, use_scope
from tests_helpers import register_and_login


# ---------- 范围表达式 ----------

def test_scope_without_kb_sees_own_plus_public():
    where = RagScope(user_id=7).to_where()
    assert where == {"$or": [{"user_id": 7}, {"is_public": True}]}


def test_scope_with_kb_is_double_condition():
    where = RagScope(user_id=7, knowledge_base_id=3).to_where()
    assert where == {"$and": [{"user_id": 7}, {"knowledge_base_id": 3}]}


def test_use_scope_restores_after_exit():
    assert current_scope() is None

    with use_scope(1, 2):
        assert current_scope() == RagScope(1, 2)
        with use_scope(3):
            assert current_scope() == RagScope(3, None)
        # 内层退出后回到外层，而不是变回 None
        assert current_scope() == RagScope(1, 2)

    assert current_scope() is None


def test_use_scope_restores_on_exception():
    with pytest.raises(RuntimeError):
        with use_scope(1, 2):
            raise RuntimeError("boom")
    assert current_scope() is None


# ---------- 服务缓存 ----------

def test_service_is_cached_per_scope(monkeypatch):
    local_tool.clear_rag_cache()
    built: list[dict | None] = []

    class _FakeService:
        def __init__(self, where=None):
            built.append(where)

        def rag_summarize(self, query):
            return f"answer:{query}"

    monkeypatch.setattr(local_tool, "_build", lambda scope: _FakeService(
        None if scope is None else scope.to_where()
    ))

    with use_scope(1, 5):
        first = local_tool.get_rag_service()
        second = local_tool.get_rag_service()
    assert first is second
    assert built == [{"$and": [{"user_id": 1}, {"knowledge_base_id": 5}]}]

    with use_scope(1, 6):
        third = local_tool.get_rag_service()
    assert third is not first
    assert len(built) == 2

    local_tool.clear_rag_cache()


# ---------- 工具读取当前范围 ----------

def test_local_tool_uses_request_scope(monkeypatch):
    local_tool.clear_rag_cache()
    seen: list[dict | None] = []

    class _FakeService:
        def rag_summarize(self, query):
            return "ok"

    def _fake_get_service(scope=None):
        scope = scope or current_scope()
        seen.append(None if scope is None else scope.to_where())
        return _FakeService()

    monkeypatch.setattr(local_tool, "get_rag_service", _fake_get_service)

    with use_scope(9, 4):
        assert local_tool.local_rag_summarize({"query": "滤网多久换"}) == "ok"

    # 没有请求上下文时退回全局范围（命令行 / Streamlit 旧路径）
    local_tool.local_rag_summarize({"query": "x"})

    assert seen == [{"$and": [{"user_id": 9}, {"knowledge_base_id": 4}]}, None]


def test_local_tool_rejects_empty_query():
    assert "参数错误" in local_tool.local_rag_summarize({})


# ---------- 接口：归属校验与范围下发 ----------

class _ScopeProbeAgent:
    """假 Agent：记下执行时 ContextVar 里的范围，不调模型。"""

    def __init__(self, user_id: int, session_id: str, seen: list):
        self.user_id = user_id
        self.session_id = session_id
        self.seen = seen

    async def execute_stream(self, query, mcp_manager=None):
        self.seen.append(current_scope())
        yield "ok"

    def clear_history(self):
        pass


class _FakeManager:
    mcp_mgr = None
    is_ready = True

    def __init__(self, seen: list):
        self._seen = seen

    def get_agent(self, user_id: int, session_id=None):
        sid = (session_id or "").strip() or "default"
        return _ScopeProbeAgent(user_id, sid, self._seen)


@pytest.fixture()
def chat_env(tmp_path, monkeypatch):
    engine = build_engine(f"sqlite:///{(tmp_path / 'scope.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", TestingSession)

    seen: list = []
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_agent_manager] = lambda: _FakeManager(seen)
    with TestClient(app) as client:
        yield client, seen
    engine.dispose()


def _create_kb(client, headers, name):
    resp = client.post("/knowledge-bases", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_chat_rejects_other_users_kb(chat_env):
    client, seen = chat_env
    alice = register_and_login(client, "s-alice@test.local")
    bob = register_and_login(client, "s-bob@test.local")
    kb = _create_kb(client, alice["headers"], "alice-b")

    resp = client.post(
        "/chat", json={"query": "hi", "knowledge_base_id": kb["id"]}, headers=bob["headers"]
    )
    assert resp.status_code == 404
    # 关键：连 Agent 都没启动，更没有产生任何检索
    assert seen == []


def test_chat_passes_own_kb_scope_to_agent(chat_env):
    client, seen = chat_env
    user = register_and_login(client, "s-owner@test.local")
    kb = _create_kb(client, user["headers"], "own-b")

    resp = client.post(
        "/chat", json={"query": "hi", "knowledge_base_id": kb["id"]}, headers=user["headers"]
    )
    assert resp.status_code == 200
    assert seen == [RagScope(user["user_id"], kb["id"])]


def test_chat_without_kb_scopes_to_user_only(chat_env):
    client, seen = chat_env
    user = register_and_login(client, "s-nokb@test.local")

    resp = client.post("/chat", json={"query": "hi"}, headers=user["headers"])
    assert resp.status_code == 200
    assert seen == [RagScope(user["user_id"], None)]
