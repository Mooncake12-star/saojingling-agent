"""P1 第一步的冒烟测试：ORM 模型 + Alembic 迁移链路。

为什么先写这些：
    下一步要把 8 处 get_connection() 调用点改写成 repository。没有回归测试兜底，
    重构就是闭眼改。这几个用例只锁「工具链能跑通」，不测业务逻辑。

隔离：全部使用 pytest 的 tmp_path 建临时库，不碰 data/zst.db 和 data/app.db。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config

from db.base import Base
from db.session import build_engine
from models.product import Product

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _session_on(tmp_path: Path) -> tuple[Session, object]:
    """在临时文件上建库并返回 Session。

    用文件而不是 :memory:，是因为 :memory: 每条连接都是独立的库，
    create_all 建的表和后面查询用的连接可能不是同一个。
    """
    engine = build_engine(f"sqlite:///{(tmp_path / 'orm.db').as_posix()}")
    Base.metadata.create_all(engine)
    return Session(bind=engine), engine


def test_metadata_contains_products_table():
    """模型必须注册到 Base.metadata，否则 Alembic 会生成空迁移。"""
    assert "products" in Base.metadata.tables
    columns = set(Base.metadata.tables["products"].columns.keys())
    assert {
        "id", "brand", "name", "type", "price",
        "specs", "features", "suitable_for", "rating",
    } <= columns


def test_alembic_upgrade_creates_products(tmp_path, monkeypatch):
    """跑一遍真实的迁移脚本，验证 upgrade 能建出表并写入版本号。"""
    db_file = tmp_path / "migrate.db"
    monkeypatch.setenv("ZST_DB_URL", f"sqlite:///{db_file.as_posix()}")

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db_file)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "products" in tables
        assert "alembic_version" in tables, "版本表没建出来，说明迁移没真正执行"

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version and version[0], "alembic_version 为空，current 会是空白"

        cols = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
        assert {"id", "brand", "name", "specs"} <= cols
    finally:
        conn.close()


def test_product_roundtrip_and_json_helpers(tmp_path):
    """写入-读回，并验证 JSON 字段的解析属性。"""
    session, engine = _session_on(tmp_path)
    try:
        session.add(
            Product(
                id="p1",
                brand="智扫",
                name="X1 扫地机器人",
                type="扫地机器人",
                price=2699.0,
                specs='{"吸力": "5000Pa"}',
                features='["激光导航", "自动集尘"]',
                suitable_for='["有宠家庭"]',
                rating=4.8,
            )
        )
        session.commit()

        # 换一个 Session 查询，确认数据真的落盘而不是留在 identity map 里
        with Session(bind=engine) as fresh:
            p = fresh.scalars(select(Product).where(Product.id == "p1")).one()
            assert p.name == "X1 扫地机器人"
            assert p.specs_dict == {"吸力": "5000Pa"}
            assert p.features_list == ["激光导航", "自动集尘"]
            assert p.suitable_for_list == ["有宠家庭"]
    finally:
        session.close()
        engine.dispose()


def test_name_not_null_is_enforced(tmp_path):
    """name 在模型上是 NOT NULL，约束必须真的落到库里，而不只是写在代码里。"""
    session, engine = _session_on(tmp_path)
    try:
        session.add(Product(id="p2", name=None))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_broken_json_does_not_break_read(tmp_path):
    """历史脏数据（非 JSON）不能让整条查询失败，只退回默认值。"""
    session, engine = _session_on(tmp_path)
    try:
        session.add(Product(id="p3", name="脏数据样本", specs="not-a-json", features=None))
        session.commit()

        p = session.get(Product, "p3")
        assert p.specs_dict == {}
        assert p.features_list == []
    finally:
        session.close()
        engine.dispose()


def test_session_scope_rolls_back_on_error(tmp_path, monkeypatch):
    """异常路径必须回滚：这是替换掉裸 sqlite3 连接后最需要保证的行为。"""
    from db import session as session_module

    engine = build_engine(f"sqlite:///{(tmp_path / 'scope.db').as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(session_module, "SessionLocal", session_module.sessionmaker(bind=engine))

    with pytest.raises(RuntimeError):
        with session_module.session_scope() as s:
            s.add(Product(id="p4", name="会被回滚"))
            raise RuntimeError("模拟中途失败")

    with Session(bind=engine) as check:
        assert check.get(Product, "p4") is None

    engine.dispose()
