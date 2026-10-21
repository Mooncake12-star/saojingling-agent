"""会话记忆相关的两张表。

业务背景（对应 agent/react_agent.py 的记忆机制）：
    conversations        每条消息一行，是原文
    conversation_summary 每个 session 一行，是「超出滑动窗口的旧消息」压成的摘要

P2 之后两者都带 user_id：数据必须有主。
    conversations.user_id      → 真实外键指向 users.id，ON DELETE CASCADE
    conversation_summary 主键   → 复合主键 (user_id, session_id)

为什么摘要表要改主键：
    P2 之前 session_id 是全局唯一的，所以能做单列主键。
    有了用户之后，两个人都用 "default" 是合法的——它们是两个不同的会话。
    如果继续用 session_id 单列主键，第二个人会覆盖第一个人的摘要，
    而且不报错。这类「主键选错」的坑只会在多用户下暴露。

为什么 conversations 现在能有真外键了：
    以前 conversations.session_id 既非主键也不唯一，没东西可指向。
    现在 user_id 指向 users.id，是真的引用完整性问题，交给数据库约束比靠代码自觉可靠。
    ON DELETE CASCADE 让「删用户」这件事不会留下孤儿消息。

时间字段统一由数据库生成（server_default=CURRENT_TIMESTAMP），不在 Python 里取时间：
    多实例部署时各机器时钟不一致，由写入方生成时间会出现「消息时间倒流」。
    注意 SQLite 的 CURRENT_TIMESTAMP 是 UTC，比北京时间晚 8 小时。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from db.base import Base


class Conversation(Base):
    """一条对话消息（用户或助手）。"""

    __tablename__ = "conversations"
    # 查询模式固定是 WHERE user_id=? AND session_id=? AND id>? ORDER BY id
    # 复合索引前缀命中 user_id + session_id，id 就是 rowid 自带排序，一条索引够用
    __table_args__ = (
        Index("ix_conversations_user_session", "user_id", "session_id"),
    )

    # SQLite 里 INTEGER PRIMARY KEY 就是 rowid，自增且天然有序，
    # 排序用它比用 created_at 可靠（同一秒内写入的多条消息时间戳相同）。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    def __repr__(self) -> str:
        preview = (self.content or "")[:20]
        return (
            f"<Conversation id={self.id} user={self.user_id} "
            f"session={self.session_id!r} role={self.role!r} {preview!r}>"
        )


class ConversationSummary(Base):
    """一个 session 的滚动摘要。

    folded_until_id 表示「id 小于等于它的消息已经折进 summary」，
    加载历史时只取 id 更大的那部分，避免同一段对话被重复折叠。
    注意 id 是全局自增的（不是每个会话从 1 开始），所以它只在同一会话内可比。
    """

    __tablename__ = "conversation_summary"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    summary: Mapped[str | None] = mapped_column(Text)
    folded_until_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    # 逻辑关联，不是数据库外键：conversations 侧没有 (user_id, session_id) 唯一约束，
    # 一对多只能在 SQL 里 join 表达。所以必须 viewonly：
    # 往 messages 里 append 不会写库，要写必须自己 add(Conversation(...))。
    messages: Mapped[list[Conversation]] = relationship(
        Conversation,
        primaryjoin=(
            "and_(ConversationSummary.session_id == foreign(Conversation.session_id), "
            "ConversationSummary.user_id == foreign(Conversation.user_id))"
        ),
        order_by="Conversation.id",
        viewonly=True,
    )

    def __repr__(self) -> str:
        return (
            f"<ConversationSummary user={self.user_id} session={self.session_id!r} "
            f"folded_until_id={self.folded_until_id}>"
        )
