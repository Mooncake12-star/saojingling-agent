"""知识库表。

P3 引入的第一张「业务对象」表：一个用户可以有多个知识库，每个知识库下挂多个文档。

为什么知识库要独立成表，而不是直接在 documents 上打一个字符串标签：
    「库」本身有生命周期——改名、改描述、整个删掉。如果只靠 documents.kb_name 字符串，
    改一次名要把所有文档行都 update 一遍，删库时也无法表达「这个库存在但还没文档」。
    有独立表之后，「空库」是一个合法状态，前端建完库立刻能显示出来。

为什么 (user_id, name) 唯一：
    同一个用户建两个同名知识库，在界面上无法区分，检索时也无从选择。
    这条约束放在数据库里而不是查一次再插入——两个请求同时建同名库时，
    先查后插会都通过检查，只有唯一约束能挡住（代价是路由要处理 IntegrityError）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class KnowledgeBase(Base):
    """一个用户的一个知识库。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_knowledge_bases_user_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ON DELETE CASCADE：删用户时库跟着走，SqlAlchemy 外键 + PRAGMA foreign_keys=ON 生效
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    def __repr__(self) -> str:
        return f"<KnowledgeBase id={self.id} user={self.user_id} name={self.name!r}>"
