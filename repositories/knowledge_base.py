"""知识库的数据访问。

和 ConversationRepository 同一套约定：
    - user_id 是**必填位置参数**，漏传直接 TypeError，不给「忘加条件」的机会
    - 只写 SQL，不做业务判断，不提交事务

「查不到」和「不是你的」在这里统一返回 None：
    上层据此返回 404，而不是 403。403 等于告诉攻击者「这个 id 确实存在，
    只是不属于你」，等于送了一个资源枚举接口。语义上也更准确——
    对当前用户而言，别人的知识库本来就不该存在。
"""
from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from models.document import Document
from models.knowledge_base import KnowledgeBase


class KnowledgeBaseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, user_id: int, name: str, description: str | None = None) -> KnowledgeBase:
        kb = KnowledgeBase(user_id=user_id, name=name, description=description)
        self.session.add(kb)
        self.session.flush()
        return kb

    def get(self, user_id: int, kb_id: int) -> KnowledgeBase | None:
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.user_id == user_id,
        )
        return self.session.scalars(stmt).first()

    def list_for_user(self, user_id: int) -> list[tuple[KnowledgeBase, int]]:
        """该用户的全部知识库及其文档数，新建的排在前面。

        文档数用一条 LEFT JOIN + GROUP BY 拿到，而不是「先查库再逐个 count」：
        后者是 N+1 查询，库多了之后列表页会变成几十次往返。
        LEFT JOIN 保证空库也在结果里（count 为 0）。
        """
        stmt = (
            select(KnowledgeBase, func.count(Document.id))
            .outerjoin(
                Document,
                and_(
                    Document.knowledge_base_id == KnowledgeBase.id,
                    Document.user_id == user_id,
                ),
            )
            .where(KnowledgeBase.user_id == user_id)
            .group_by(KnowledgeBase.id)
            .order_by(KnowledgeBase.id.desc())
        )
        return [(kb, count) for kb, count in self.session.execute(stmt)]

    def delete(self, user_id: int, kb_id: int) -> bool:
        """删除知识库，返回是否真的删掉了一行。

        documents 的级联删除交给数据库外键：db/session.py 里已经
        PRAGMA foreign_keys=ON，ON DELETE CASCADE 才会真正生效。
        （SQLite 默认是关的，这是最容易踩的一个坑：不开这个 PRAGMA，
          SQL 不会报错，孤儿行会安静地留下来。）
        """
        kb = self.get(user_id, kb_id)
        if kb is None:
            return False
        self.session.delete(kb)
        self.session.flush()
        return True

    def count_documents(self, user_id: int, kb_id: int) -> int:
        stmt = select(func.count(Document.id)).where(
            Document.knowledge_base_id == kb_id,
            Document.user_id == user_id,
        )
        return int(self.session.scalar(stmt) or 0)
