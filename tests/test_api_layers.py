"""分层收口后的接口测试。

覆盖三件最容易回归的事：
    1. schema 能否把库里的 JSON 字符串正确解析成结构，脏数据不炸
    2. 路由是否真的走 repository + schema（而不是又把 ORM 对象直接抛出去）
    3. 上传/下载的路径穿越防护

用 dependency_overrides 注入临时库和假 Agent，不启动真实 MCP：
启动 MCP 要拉起子进程 + 加载重排模型（1.5GB），测试里做这件事毫无意义。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from db.base import Base
from db.session import build_engine, get_db
from fastapi_app.api import api_router
from fastapi_app.api.dependencies import get_current_user, require_agent_manager
from fastapi_app.services import chat as chat_service
from fastapi_app.services.files import _safe_name, resolve_report
from models.product import Product
from models.user import User
from schemas import ProductRead


# ---------- schema ----------

def test_product_schema_parses_json_columns():
    row = Product(
        id="x1",
        brand="智扫",
        name="智扫 X1 Pro",
        price=2499.0,
        specs='{"suction": "5000Pa"}',
        features='["激光导航", "自动集尘"]',
        suitable_for='["中户型（90-140㎡）"]',
    )
    out = ProductRead.model_validate(row).model_dump()

    assert out["specs"] == {"suction": "5000Pa"}
    assert out["features"] == ["激光导航", "自动集尘"]
    assert out["suitable_for"] == ["中户型（90-140㎡）"]
    assert out["name"] == "智扫 X1 Pro"


@pytest.mark.parametrize("raw", [None, "", "not-json", "[]", '{"a": 1}'])
def test_product_schema_tolerates_dirty_json(raw):
    """脏数据不能让整个列表接口 500：类型不对就退回空值。"""
    row = Product(id="d1", name="脏数据", specs=raw, features=raw, suitable_for=raw)
    out = ProductRead.model_validate(row).model_dump()

    assert isinstance(out["specs"], dict)
    assert isinstance(out["features"], list)
    assert isinstance(out["suitable_for"], list)


def test_product_schema_accepts_already_parsed_value():
    """schema 也要能吃 dict/list：以后 repository 直接返回 dict 时不用改接口。"""
    out = ProductRead.model_validate(
        {"id": "x1", "name": "n", "specs": {"a": 1}, "features": ["f"]}
    )
    assert out.specs == {"a": 1}
    assert out.features == ["f"]


# ---------- services ----------

def test_build_query_text_appends_hint_only_when_file_exists(tmp_path):
    img = tmp_path / "a.jpg"
    img.write_bytes(b"fake")

    with_img = chat_service.build_query_text("这是什么", str(img))
    assert "这是什么" in with_img
    assert "image_analyze" in with_img

    missing = chat_service.build_query_text("这是什么", str(tmp_path / "nope.jpg"))
    assert missing == "这是什么"


class _FakeAgent:
    async def execute_stream(self, query, mcp_manager=None):
        for chunk in ("你", "好"):
            yield chunk


class _FakeManager:
    """只实现服务层用到的两个成员。"""

    mcp_mgr = None
    is_ready = True

    def __init__(self) -> None:
        self.queries: list[str] = []

    def get_agent(self, user_id, session_id=None):
        self.last_key = (user_id, session_id)
        return _FakeAgent()


@pytest.mark.asyncio
async def test_stream_sse_frames():
    frames = [f async for f in chat_service.stream_sse(1, "hi", agent_manager=_FakeManager())]

    assert frames[0] == "data: 你\n\n".encode()
    assert frames[-1] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_collect_answer_joins_chunks():
    answer = await chat_service.collect_answer(1, "hi", agent_manager=_FakeManager())
    assert answer == "你好"


def _fake_user():
    """不落库的用户对象：鉴权依赖被覆盖后，路由只用到 id。"""
    return User(id=1, email="tester@test.local", password_hash="x", role="user", is_active=True)


# ---------- 路由 ----------

@pytest.fixture()
def client(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with TestingSession() as s:
        s.add(Product(id="x1", brand="智扫", name="智扫 X1 Pro", price=2499.0,
                      specs='{"suction": "5000Pa"}', features='["激光导航"]'))
        s.add(Product(id="m1", brand="智扫", name="智扫 M1 Lite", price=1899.0))
        s.add(Product(id="o1", brand="其他", name="别的牌子", price=999.0))
        s.commit()

    def _override_db():
        with TestingSession() as s:
            yield s

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_agent_manager] = lambda: _FakeManager()
    app.dependency_overrides[get_current_user] = lambda: _fake_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_products_uses_schema(client):
    resp = client.get("/products")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    # 按 id 排序（repository 显式 order_by），别假设插入顺序
    x1 = next(p for p in body if p["id"] == "x1")
    # 响应里 specs 是对象而不是 JSON 字符串，说明确实过了 schema
    assert x1["specs"] == {"suction": "5000Pa"}
    assert x1["features"] == ["激光导航"]


def test_list_products_brand_filter(client):
    resp = client.get("/products", params={"brand": "智扫"})
    assert [p["id"] for p in resp.json()] == ["m1", "x1"]


def test_list_products_limit_validation(client):
    assert client.get("/products", params={"limit": 0}).status_code == 422


def test_product_detail_404(client):
    resp = client.get("/products/不存在")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "产品不存在"


def test_chat_returns_full_answer(client):
    resp = client.post("/chat", json={"query": "hi", "session_id": "s1"})
    assert resp.status_code == 200
    assert resp.json() == {"answer": "你好"}


def test_chat_stream_is_sse(client):
    resp = client.post("/chat/stream", json={"query": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in resp.text


def test_chat_requires_login(client):
    """去掉鉴权覆盖后必须 401，而不是把 session_id 当成身份。"""
    client.app.dependency_overrides.pop(get_current_user, None)
    assert client.post("/chat", json={"query": "hi"}).status_code == 401
    assert client.post("/chat/stream", json={"query": "hi"}).status_code == 401


def test_upload_requires_login(client):
    client.app.dependency_overrides.pop(get_current_user, None)
    resp = client.post("/upload", files={"file": ("a.txt", b"x")})
    assert resp.status_code == 401


def test_products_stay_public(client):
    """产品目录是公共数据，不跟着一起收进登录墙后。"""
    client.app.dependency_overrides.pop(get_current_user, None)
    assert client.get("/products").status_code == 200


def test_chat_unavailable_when_agent_not_ready(client):
    """去掉就绪覆盖后应当 503，而不是 500。"""
    client.app.dependency_overrides.pop(require_agent_manager, None)
    resp = client.post("/chat", json={"query": "hi"})
    assert resp.status_code == 503


# ---------- 文件安全 ----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a.jpg", "a.jpg"),
        ("../../config/agent.yml", "agent.yml"),
        ("..\\..\\config\\agent.yml", "agent.yml"),
        ("/etc/passwd", "passwd"),
    ],
)
def test_safe_name_strips_directories(raw, expected):
    assert _safe_name(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", ".", ".."])
def test_safe_name_rejects_empty(raw):
    with pytest.raises(ValueError):
        _safe_name(raw)


def test_resolve_report_blocks_traversal():
    with pytest.raises(FileNotFoundError):
        resolve_report("../../config/agent.yml")
