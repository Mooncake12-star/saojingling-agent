"""会话记忆两张表的模型测试。

只覆盖容易出错、且后面重构会依赖的五个点：
    1. 一对多 relationship 能否正确加载、是否按 id 排序
    2. viewonly 的语义：往 messages 里 append 不会落库（防止以后误用）
    3. created_at / folded_until_id 是否真的由数据库或模型填上默认值
    4. 「只取未折叠消息」这个查询模式在新模型上是否还成立
    5. 复合主键 (user_id, session_id)：同一个 session_id 在两个人名下互不覆盖

表结构由 Base.metadata.create_all 直接建，不经过 Alembic——
迁移链路本身已由 test_orm_smoke.py 覆盖，这里只测模型行为，跑得更快。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.base import Base
from db.session import build_engine
from models.conversation import Conversation, ConversationSummary
from models.user import User


@pytest.fixture()
def engine(tmp_path):
    eng = build_engine(f"sqlite:///{(tmp_path / 'conv.db').as_posix()}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def uid(engine):
    """数据归属人。user_id 是 NOT NULL 外键，没有用户什么都插不进去。"""
    with Session(bind=engine) as s:
        user = User(email="model@test.local", password_hash="x")
        s.add(user)
        s.commit()
        return user.id


def test_messages_relationship_ordered_and_scoped(engine, uid):
    """relationship 只带出本 session 的消息，且按 id 升序。"""
    with Session(bind=engine) as s:
        s.add_all([
            Conversation(user_id=uid, session_id="s1", role="human", content="第一句"),
            Conversation(user_id=uid, session_id="s2", role="human", content="别的会话"),
            Conversation(user_id=uid, session_id="s1", role="ai", content="第二句"),
        ])
        s.add(ConversationSummary(user_id=uid, session_id="s1", summary="摘要"))
        s.commit()

    with Session(bind=engine) as s:
        summary = s.get(ConversationSummary, (uid, "s1"))
        assert [m.content for m in summary.messages] == ["第一句", "第二句"]
        assert all(m.session_id == "s1" for m in summary.messages)


def test_relationship_does_not_cross_users(engine, uid):
    """主键换成 (user_id, session_id) 后，join 两个条件缺一不可。

    只按 session_id 关联的话，A 的摘要会把 B 同名会话的消息也带出来——
    这种错误在单用户测试里永远发现不了。
    """
    with Session(bind=engine) as s:
        other = User(email="other@test.local", password_hash="x")
        s.add(other)
        s.flush()
        s.add_all([
            Conversation(user_id=uid, session_id="dup", role="human", content="A 说的"),
            Conversation(user_id=other.id, session_id="dup", role="human", content="B 说的"),
            ConversationSummary(user_id=uid, session_id="dup", summary="A 的摘要"),
            ConversationSummary(user_id=other.id, session_id="dup", summary="B 的摘要"),
        ])
        s.commit()

        a_summary = s.get(ConversationSummary, (uid, "dup"))
        assert [m.content for m in a_summary.messages] == ["A 说的"]


def test_viewonly_relationship_does_not_persist(engine, uid):
    """viewonly 的集合是可读视图：append 进去的对象不会被写库。

    这正是没有 (user_id, session_id) 唯一约束时的正确语义，也提醒使用者：
    要写消息必须 session.add(Conversation(...))，不能靠 append。
    """
    with Session(bind=engine) as s:
        s.add(ConversationSummary(user_id=uid, session_id="s3", summary="x"))
        s.commit()

        summary = s.get(ConversationSummary, (uid, "s3"))
        summary.messages.append(
            Conversation(user_id=uid, session_id="s3", role="human", content="凭空加的")
        )
        s.commit()

    with Session(bind=engine) as s:
        count = s.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.user_id == uid, Conversation.session_id == "s3")
        )
        assert count == 0


def test_defaults_filled_by_db(engine, uid):
    """created_at / updated_at 由数据库 CURRENT_TIMESTAMP 生成，folded_until_id 默认 0。"""
    with Session(bind=engine) as s:
        s.add(Conversation(user_id=uid, session_id="s4", role="human", content="hi"))
        s.add(ConversationSummary(user_id=uid, session_id="s4"))
        s.commit()

    with Session(bind=engine) as s:
        msg = s.scalars(
            select(Conversation).where(Conversation.user_id == uid, Conversation.session_id == "s4")
        ).one()
        assert isinstance(msg.created_at, datetime)

        summary = s.get(ConversationSummary, (uid, "s4"))
        assert summary.folded_until_id == 0
        assert isinstance(summary.updated_at, datetime)


def test_window_query_skips_folded_messages(engine, uid):
    """复刻 react_agent._load_history 的查询：只取 id > folded_until_id 的消息。"""
    with Session(bind=engine) as s:
        for i in range(5):
            s.add(Conversation(user_id=uid, session_id="s5", role="human", content=f"msg-{i}"))
        s.commit()

        ids = s.scalars(
            select(Conversation.id)
            .where(Conversation.user_id == uid, Conversation.session_id == "s5")
            .order_by(Conversation.id)
        ).all()
        folded_until_id = ids[2]  # 前三条已折进摘要

        s.add(
            ConversationSummary(
                user_id=uid, session_id="s5", summary="前面三条的摘要",
                folded_until_id=folded_until_id,
            )
        )
        s.commit()

    with Session(bind=engine) as s:
        remaining = s.scalars(
            select(Conversation.content)
            .where(
                Conversation.user_id == uid,
                Conversation.session_id == "s5",
                Conversation.id > folded_until_id,
            )
            .order_by(Conversation.id)
        ).all()
        assert remaining == ["msg-3", "msg-4"]
