"""文档的数据访问。

状态流转也只在这里写 SQL，service 只负责说「把它标成 ready / failed」。
好处是将来加「处理耗时」「重试次数」这类字段，只改这一个文件。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.document import Document


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        user_id: int,
        knowledge_base_id: int,
        filename: str,
        stored_path: str,
        md5: str,
        size_bytes: int,
    ) -> Document:
        doc = Document(
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            stored_path=stored_path,
            md5=md5,
            size_bytes=size_bytes,
        )
        self.session.add(doc)
        self.session.flush()
        return doc

    def get(self, user_id: int, doc_id: int) -> Document | None:
        stmt = select(Document).where(Document.id == doc_id, Document.user_id == user_id)
        return self.session.scalars(stmt).first()

    def get_by_id(self, doc_id: int) -> Document | None:
        """不做 user 过滤，**只允许后台任务使用**。

        后台任务拿到的 document_id 是上传接口自己写进队列的，不是用户输入，
        所以不需要再过一遍身份；反过来，这里绝对不能出现在任何路由里。
        """
        return self.session.get(Document, doc_id)

    def get_by_md5(self, knowledge_base_id: int, md5: str) -> Document | None:
        """同一知识库内按内容去重。跨库不去重：两个库各自维护自己的副本是合理的。"""
        stmt = select(Document).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.md5 == md5,
        )
        return self.session.scalars(stmt).first()

    def list_for_kb(self, user_id: int, kb_id: int) -> list[Document]:
        stmt = (
            select(Document)
            .where(Document.knowledge_base_id == kb_id, Document.user_id == user_id)
            .order_by(Document.id.desc())
        )
        return list(self.session.scalars(stmt))

    def set_status(
        self,
        doc_id: int,
        status: str,
        *,
        error_message: str | None = None,
        chunk_count: int | None = None,
    ) -> None:
        """更新状态。显式写 updated_at，避免依赖数据库 ON UPDATE（SQLite 没有）。

        一条 SQL 直接 UPDATE，而不是「get 出来改属性」：
        后台任务和请求线程各持一个 Session，读-改-写会互相覆盖。
        """
        values: dict = {"status": status, "updated_at": func.current_timestamp()}
        if error_message is not None:
            values["error_message"] = error_message
        if chunk_count is not None:
            values["chunk_count"] = chunk_count
        self.session.execute(
            Document.__table__.update().where(Document.id == doc_id).values(**values)
        )

    def reset_for_reingest(self, doc_id: int, stored_path: str) -> None:
        """重新入库：把状态和上一次的错误清干净，避免前端看到失败残留。"""
        self.session.execute(
            Document.__table__.update()
            .where(Document.id == doc_id)
            .values(
                status="pending",
                error_message=None,
                chunk_count=0,
                stored_path=stored_path,
                updated_at=func.current_timestamp(),
            )
        )

    def delete(self, user_id: int, doc_id: int) -> bool:
        doc = self.get(user_id, doc_id)
        if doc is None:
            return False
        self.session.delete(doc)
        self.session.flush()
        return True

    def list_all_for_kb(self, kb_id: int) -> list[Document]:
        """不按 user 过滤，供「删知识库前清理向量」这类后台流程使用。"""
        stmt = select(Document).where(Document.knowledge_base_id == kb_id)
        return list(self.session.scalars(stmt))
