"""产品数据访问层 + MCP 工具接入的测试。

分两组：
    1. ProductRepository 的单元测试：排序、命中、LIKE 通配符转义
    2. 集成测试：真正调用 mcp_server 的 price_query / product_recommend，
       确认换成 repository 之后输出没变

集成测试用临时库替换 db.session.SessionLocal，不依赖 data/app.db 是否已 seed。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from db.base import Base
from db.session import build_engine
from models.product import Product
from repositories.product import ProductRepository, escape_like

PRODUCTS = [
    Product(
        id="x1",
        brand="智扫",
        name="智扫 X1 Pro",
        type="扫地机器人",
        price=2499.0,
        specs='{"suction": "5000Pa", "navigation": "激光导航"}',
        features='["激光导航"]',
        suitable_for='["中户型（90-140㎡）"]',
        rating=4.5,
    ),
    Product(
        id="m1",
        brand="智扫",
        name="智扫 M1 Lite",
        type="扫地机器人",
        price=1899.0,
        specs='{"suction": "3500Pa", "navigation": "陀螺仪"}',
        features='["陀螺仪规划"]',
        suitable_for='["小户型（<90㎡）"]',
        rating=4.1,
    ),
    # 专门用来验证 LIKE 转义：名字里带 % 和 _
    Product(
        id="sale_100%",
        brand="清理",
        name="100% 清洁款",
        type="扫拖一体机器人",
        price=999.0,
        specs="{}",
        features="[]",
        suitable_for="[]",
        rating=3.9,
    ),
]


@pytest.fixture()
def session(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'repo.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(bind=engine) as s:
        # 每个 Product 对象只能挂在一个 session 上，所以这里重新构造
        for p in PRODUCTS:
            s.add(Product(**{c.name: getattr(p, c.name) for c in Product.__table__.columns}))
        s.commit()
        yield s
    engine.dispose()


def test_list_all_sorted_by_id(session):
    rows = ProductRepository(session).list_all()
    assert [p.id for p in rows] == sorted(p.id for p in rows)


def test_get_by_primary_key(session):
    repo = ProductRepository(session)
    assert repo.get("x1").name == "智扫 X1 Pro"
    assert repo.get("nope") is None


def test_search_is_case_insensitive(session):
    repo = ProductRepository(session)
    assert [p.id for p in repo.search_by_name("x1 pro")] == ["x1"]
    assert [p.id for p in repo.search_by_name("M1")] == ["m1"]


def test_search_matches_id_too(session):
    repo = ProductRepository(session)
    assert [p.id for p in repo.search_by_name("sale")] == ["sale_100%"]


def test_search_escapes_like_wildcards(session):
    """搜 % 只能命中名字里真的含 % 的那条，不能匹配全部。

    这是 LIKE 最容易被忽略的坑：不转义的话 "%" 拼成 "%%%" 等于全表匹配。
    """
    repo = ProductRepository(session)
    hits = repo.search_by_name("%")
    assert [p.id for p in hits] == ["sale_100%"]

    # 下划线同理：不转义的话 "_" 会匹配任意单字符
    hits = repo.search_by_name("_")
    assert [p.id for p in hits] == ["sale_100%"]


def test_search_empty_keyword_returns_empty(session):
    assert ProductRepository(session).search_by_name("   ") == []


def test_escape_like_order():
    """反斜杠必须最先转义，否则会把新加的转义符再转一遍。"""
    assert escape_like("50%_x") == "50\\%\\_x"
    assert escape_like("a\\b") == "a\\\\b"


# ──────────────────────────────────────────────
# 集成：MCP 工具改用 repository 后行为不变
# ──────────────────────────────────────────────
@pytest.fixture()
def patched_tool_db(tmp_path, monkeypatch):
    from db import session as session_module

    engine = build_engine(f"sqlite:///{(tmp_path / 'tool.db').as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        session_module, "SessionLocal", session_module.sessionmaker(bind=engine)
    )
    with Session(bind=engine) as s:
        for p in PRODUCTS:
            s.add(Product(**{c.name: getattr(p, c.name) for c in Product.__table__.columns}))
        s.commit()
    yield engine
    engine.dispose()


def test_price_query_returns_product(patched_tool_db):
    from agent.tools.mcp_server import price_query

    out = price_query("X1")
    assert "智扫 X1 Pro" in out
    assert "2499" in out
    assert "5000Pa" in out


def test_price_query_lists_options_when_not_found(patched_tool_db):
    from agent.tools.mcp_server import price_query

    out = price_query("不存在的型号")
    assert "未找到产品" in out
    assert "智扫 M1 Lite" in out  # 兜底时列出全部可选产品


def test_product_recommend_still_works(patched_tool_db):
    from agent.tools.mcp_server import product_recommend

    out = product_recommend(house_size="中户型（90-140㎡）", budget=3000)
    assert "为您推荐以下产品" in out
    assert "智扫 X1 Pro" in out
