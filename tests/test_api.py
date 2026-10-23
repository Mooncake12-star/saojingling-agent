import os, sys
import uuid

import pytest


def _ensure_default_db_tables():
    """为直接构造 ReactAgent 的用例兜底建表。

    ReactAgent 构造时会读会话历史（load_window），默认库文件是
    data/app.db。本地开发环境这个文件早已存在、表也齐全；但 CI 的
    干净 checkout 里既没有这个文件也没有表结构（它被 gitignore，
    表结构由 Alembic 管理），构造 Agent 就会报 no such table。

    这里对默认 engine 幂等建一次表：本地库已存在时 create_all
    是 no-op，CI 上则创建临时库文件，测试结束随 runner 丢弃。
    """
    import models  # noqa: F401 —— 必须 import，模型才会注册进 Base.metadata
    from db.base import Base
    from db.session import engine

    Base.metadata.create_all(engine)


class TestFastAPISchemas:
    def test_schemas_import(self):
        from schemas import ChatRequest, ChatResponse, StatusResponse
        assert ChatRequest is not None
        assert ChatResponse is not None
        assert StatusResponse is not None

    def test_chat_request(self):
        from schemas import ChatRequest
        req = ChatRequest(query="你好")
        assert req.query == "你好"
        assert req.image_path is None

    def test_chat_request_with_image(self):
        from schemas import ChatRequest
        req = ChatRequest(query="分析图片", image_path="/tmp/test.jpg")
        assert req.image_path == "/tmp/test.jpg"

    def test_chat_response(self):
        from schemas import ChatResponse
        resp = ChatResponse(answer="这是回答")
        assert resp.answer == "这是回答"

    def test_status_response(self):
        from schemas import StatusResponse
        resp = StatusResponse(status="ok", mcp_connected=True, rag_ready=False)
        assert resp.status == "ok"
        assert resp.mcp_connected is True
        assert resp.rag_ready is False


class TestAgentManager:
    def test_manager_import(self):
        from fastapi_app.deps import AgentManager
        manager = AgentManager()
        assert manager.is_ready is False
        # P2 起不再预建「默认会话」：没有用户就没有该建的 Agent
        assert manager._agents == {}


class TestReactAgent:
    def test_react_mcp_context_manager(self):
        from agent.react_agent import ReactMCPContextManager
        # 签名已改为 (command, args, script_dir=None)，旧用例传的 cwd 已不存在
        mgr = ReactMCPContextManager("python", ["test.py"])
        assert mgr.command == "python"
        assert mgr.session is None

    def test_agent_init(self):
        _ensure_default_db_tables()
        from agent.react_agent import ReactAgent
        # 用唯一 session_id，避免读到本地历史库导致 conversation_history 非空
        agent = ReactAgent(session_id="pytest-%s" % uuid.uuid4().hex[:8])
        assert agent.system_prompt is not None
        assert agent.conversation_history == []
