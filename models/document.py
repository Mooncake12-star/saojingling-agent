"""文档表：知识库里的一个文件及其入库状态。

这张表解决的核心问题是「上传」和「入库」不在同一时刻完成：
    上传接口只负责把文件落盘、插一行 status=pending，然后立刻返回 202；
    真正的解析、切片、向量化在后台任务里跑，跑完把 status 改成 ready。
    前端拿 document_id 轮询，就能画出「排队中 → 解析中 → 可用」的进度。

状态机只有四态，且**只能按固定方向走**：
    pending → parsing → ready
                      ↘ failed
为什么要有 parsing 这一态：
    只有 pending/ready 两态的话，用户在解析过程中看到的仍是「排队中」，
    无法区分「还没轮到」和「卡住了」。parsing 的存在让「任务被吃掉」这件事可观测。

为什么 documents 也存 user_id（明明可以从 knowledge_base 关联出来）：
    这是刻意的反范式。文档列表、状态查询是最热的两个接口，如果都要
    JOIN knowledge_bases 才能判断归属，每个查询都得记得带上 join 条件，
    漏一次就是越权。多存一列换来「所有查询都是单表 + WHERE user_id = ?」。
    代价是写入时必须从知识库的 owner 取 user_id，绝不能从请求体里取。

为什么 (knowledge_base_id, md5) 唯一：
    同一个文件重复上传时，向量库里会多出一份完全相同的 chunk，
    检索时同一段内容占据多个 Top-K 名额，直接拉低回答质量。
    唯一约束把去重交给数据库，代码只负责把冲突翻译成「已存在，直接返回」。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# 状态常量：写成常量而不是散落的字符串字面量，改名字时只改一处
DOC_PENDING = "pending"    # 已落盘，等待后台任务处理
DOC_PARSING = "parsing"    # 后台任务正在解析 / 向量化
DOC_READY = "ready"        # 已进入向量库，可被检索
DOC_FAILED = "failed"      # 处理失败，error_message 里有原因

DOC_STATUSES = (DOC_PENDING, DOC_PARSING, DOC_READY, DOC_FAILED)


class Document(Base):
    """知识库里的一个文档。"""

    __tablename__ = "documents"
    __table_args__ = (
        # SQLite 没有 ENUM 类型，用 CHECK 约束守住状态取值。
        # 写错状态名（比如 "success"）会直接插入失败，而不是变成一个永远查不到的行。
        CheckConstraint(
            "status IN ('pending', 'parsing', 'ready', 'failed')",
            # name 只写 "status"：db/base.py 的命名规范会自动拼成 ck_documents_status。
            # 写全名会变成 ck_documents_ck_documents_status（多一次前缀）
            name="status",
        ),
        UniqueConstraint("knowledge_base_id", "md5", name="uq_documents_kb_md5"),
        # 知识库详情页的查询形状：WHERE knowledge_base_id=? ORDER BY id DESC
        Index("ix_documents_kb_id", "knowledge_base_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    knowledge_base_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 落盘后的绝对路径。不存相对路径：后台任务的 cwd 与 web 进程未必一致
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    md5: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DOC_PENDING, server_default=text("'pending'")
    )
    # 失败原因要截断：异常信息里可能带很长的堆栈或路径，塞满日志没有意义
    error_message: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    # 不设 onupdate：状态更新由 repository 显式写 func.current_timestamp()，
    # 因为「改文件名」这类操作不该更新「最近处理时间」的语义
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    def __repr__(self) -> str:
        return (
            f"<Document id={self.id} kb={self.knowledge_base_id} "
            f"name={self.filename!r} status={self.status!r}>"
        )
