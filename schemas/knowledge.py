"""知识库与文档的对外契约。

三个刻意的设计：

1. 请求体里**没有** user_id
   归属永远从 token 推出来。请求体能带 user_id 的接口，等于给了一个
   「把自己的操作挂到别人账下」的入口，这类字段一律不进 schema。

2. DocumentRead 里没有 stored_path
   服务器绝对路径是内部实现，暴露出去只会帮攻击者摸清目录结构。
   前端只需要 filename 来决定显示什么、document_id 来决定轮询谁。

3. document_count 不在 Document 表里，是聚合出来的
   它不是存储事实，是某个时刻的统计值。放进 from_attributes 会报错，
   所以 KnowledgeBaseRead 用 from_model() 显式构造。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.knowledge_base import KnowledgeBase


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="知识库名称，同一用户下唯一")
    description: str | None = Field(default=None, max_length=500, description="可选描述")

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        """去掉首尾空白后再校验。

        `"   "` 这种名字能通过 min_length=1，但界面上是一个空名字，
        还占掉了唯一约束的名额。所以 strip 之后再判一次空。
        """
        value = value.strip()
        if not value:
            raise ValueError("知识库名称不能为空")
        return value


class KnowledgeBaseRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    document_count: int = 0
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, kb: KnowledgeBase, document_count: int = 0) -> "KnowledgeBaseRead":
        return cls(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            document_count=document_count,
            created_at=kb.created_at,
        )


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    knowledge_base_id: int
    filename: str
    status: str
    chunk_count: int
    size_bytes: int
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeleteResponse(BaseModel):
    message: str
    deleted_id: int
