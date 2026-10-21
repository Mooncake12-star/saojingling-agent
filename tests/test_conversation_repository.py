"""会话数据访问层 + 启动检查的测试。

重点覆盖四件容易出错的事：
    1. ON CONFLICT 的 UPDATE 分支（updated_at 是 NOT NULL，写错就抛异常）
    2. folded_until_id 过滤是否真的生效
    3. 用户隔离：同一个 session_id 在两个人名下必须是两份独立数据
    4. 启动检查能不能在缺表时给出可执行的修复提示
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from db import health
from db.base import Base
from db.session import build_engine
from models.conversation import Conversation, ConversationSummary
from models.user import User
from repositories.conversation import ConversationRepository


@pytest.fixture()
def session(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'conv_repo.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(bind=engine) as s:
        yield s
    engine.dispose()


@pytest.fixture()
def user_id(session):
    """建一个数据归属人。conversations.user_id 是 NOT NULL + 外键，没有用户就插不进去。"""
    user = User(email="owner@test.local", password_hash="x")
    session.add(user)
    session.flush()
    return user.id


@pytest.fixture()
def other_user_id(session):
    user = User(email="other@test.local", password_hash="x")
    session.add(user)
    session.flush()
    return user.id


def test_empty_session_returns_defaults(session, user_id):
    summary, folded_until_id, rows = ConversationRepository(session).load_window(user_id, "nobody")
    assert summary == ""
    assert folded_until_id == 0
    assert rows == []


def test_append_then_load(session, user_id):
    repo = ConversationRepository(session)
    first = repo.append(user_id, "s1", "human", "你好")
    second = repo.append(user_id, "s1", "ai", "你好，有什么可以帮你")
    session.commit()

    assert second > first

    summary, folded, rows = repo.load_window(user_id, "s1")
    assert summary == ""
    assert folded == 0
    assert rows == [(first, "human", "你好"), (second, "ai", "你好，有什么可以帮你")]


def test_append_returns_id_without_commit(session, user_id):
    """flush 之后就该拿到自增 id，否则调用方要等 commit 才知道 id。"""
    msg_id = ConversationRepository(session).append(user_id, "s2", "human", "x")
    assert isinstance(msg_id, int)
    assert msg_id > 0
    session.rollback()


def test_upsert_summary_insert_then_update(session, user_id):
    repo = ConversationRepository(session)
    repo.upsert_summary(user_id, "s3", "第一版摘要", 5)
    session.commit()

    row = session.get(ConversationSummary, (user_id, "s3"))
    assert row.summary == "第一版摘要"
    assert row.folded_until_id == 5

    # 第二次走 UPDATE 分支：updated_at 是 NOT NULL，这里最容易写错
    repo.upsert_summary(user_id, "s3", "第二版摘要", 9)
    session.commit()

    session.expire_all()
    row = session.get(ConversationSummary, (user_id, "s3"))
    assert row.summary == "第二版摘要"
    assert row.folded_until_id == 9
    assert row.updated_at is not None


def test_folded_messages_are_skipped(session, user_id):
    repo = ConversationRepository(session)
    ids = [repo.append(user_id, "s4", "human", f"m{i}") for i in range(5)]
    session.commit()

    repo.upsert_summary(user_id, "s4", "前三条的摘要", ids[2])
    session.commit()

    summary, folded, rows = repo.load_window(user_id, "s4")
    assert folded == ids[2]
    assert summary == "前三条的摘要"
    assert [r[0] for r in rows] == ids[3:]


def test_clear_removes_both_tables(session, user_id):
    repo = ConversationRepository(session)
    repo.append(user_id, "s5", "human", "a")
    repo.append(user_id, "s5", "ai", "b")
    repo.upsert_summary(user_id, "s5", "摘要", 2)
    session.commit()

    assert repo.clear(user_id, "s5") == 2
    session.commit()

    assert repo.count_messages(user_id, "s5") == 0
    assert session.get(ConversationSummary, (user_id, "s5")) is None

    # 只删指定会话，不能误伤别的会话
    repo.append(user_id, "s6", "human", "c")
    session.commit()
    repo.clear(user_id, "s5")
    session.commit()
    assert repo.count_messages(user_id, "s6") == 1


# ──────────────────────────────────────────────
# 用户隔离
# ──────────────────────────────────────────────
def test_same_session_id_is_isolated_between_users(session, user_id, other_user_id):
    """两个用户都用 "default"，读到的必须是各自的消息。

    这是 P2 最关键的一条：session_id 不再是全局唯一，
    隔离全靠查询里的 user_id 条件。少写一个条件，这里就会读到别人的话。
    """
    repo = ConversationRepository(session)
    repo.append(user_id, "default", "human", "A 的私密问题")
    repo.append(other_user_id, "default", "human", "B 的问题")
    session.commit()

    a_rows = repo.load_window(user_id, "default")[2]
    b_rows = repo.load_window(other_user_id, "default")[2]

    assert [r[2] for r in a_rows] == ["A 的私密问题"]
    assert [r[2] for r in b_rows] == ["B 的问题"]


def test_summary_is_isolated_between_users(session, user_id, other_user_id):
    """摘要表换成复合主键之后，同 session_id 的两个人各有一行。"""
    repo = ConversationRepository(session)
    repo.upsert_summary(user_id, "default", "A 的摘要", 1)
    repo.upsert_summary(other_user_id, "default", "B 的摘要", 2)
    session.commit()

    assert repo.load_window(user_id, "default")[0] == "A 的摘要"
    assert repo.load_window(other_user_id, "default")[0] == "B 的摘要"


def test_clear_does_not_touch_other_user(session, user_id, other_user_id):
    repo = ConversationRepository(session)
    repo.append(user_id, "shared", "human", "A")
    repo.append(other_user_id, "shared", "human", "B")
    session.commit()

    repo.clear(user_id, "shared")
    session.commit()

    assert repo.count_messages(user_id, "shared") == 0
    assert repo.count_messages(other_user_id, "shared") == 1


def test_list_sessions_only_returns_own(session, user_id, other_user_id):
    repo = ConversationRepository(session)
    repo.append(user_id, "mine-1", "human", "x")
    repo.append(user_id, "mine-1", "ai", "y")
    repo.append(user_id, "mine-2", "human", "z")
    repo.append(other_user_id, "theirs", "human", "w")
    session.commit()

    rows = dict(repo.list_sessions(user_id))
    assert rows == {"mine-1": 2, "mine-2": 1}
    assert "theirs" not in rows


# ──────────────────────────────────────────────
# 启动检查
# ──────────────────────────────────────────────
def test_ensure_schema_ready_fails_without_migration(tmp_path, monkeypatch):
    engine = build_engine(f"sqlite:///{(tmp_path / 'no_migration.db').as_posix()}")
    Base.metadata.create_all(engine)  # 表都有，但没有 alembic_version
    monkeypatch.setattr(health, "engine", engine)

    with pytest.raises(RuntimeError) as exc:
        health.ensure_schema_ready()
    assert "alembic upgrade head" in str(exc.value)

    engine.dispose()


def test_ensure_schema_ready_returns_version(tmp_path, monkeypatch):
    engine = build_engine(f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('abc123')"))
    monkeypatch.setattr(health, "engine", engine)

    assert health.ensure_schema_ready() == "abc123"

    engine.dispose()
