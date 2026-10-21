"""对话与运维相关的出入参。

为什么要有 schema 这一层：
    ORM 模型描述的是「表长什么样」，API 描述的是「对外承诺长什么样」。
    两者的变更节奏不同——表里加一个内部字段（比如 is_deleted、内部备注）
    不应该自动出现在响应里；反过来，响应里加一个组合字段（比如 full_name）
    也不该为了它去改表。

    所以路由的 response_model 一律用本目录的 schema，不直接返回 ORM 对象。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, description="用户问题")
    image_path: str | None = Field(default=None, description="/upload 返回的绝对路径")
    # 会话隔离：前端每个会话传一个自己的 id；不传则落到 "default"
    session_id: str | None = None
    # 检索范围：只在这个知识库里找答案。
    # 不传则检索「自己的文档 + 内置公共资料」——不是全库，全库会跨用户泄漏。
    # 归属仍由 token 校验：传别人的 kb_id 会得到 404，不是 403。
    knowledge_base_id: int | None = None


class ChatResponse(BaseModel):
    answer: str


class StatusResponse(BaseModel):
    status: str
    mcp_connected: bool
    rag_ready: bool
    # 缓存后端状态：off / redis / redis(circuit-open) / redis(unavailable)。
    # 放进健康检查是为了让「缓存挂了但服务还在跑」这件事在监控上一眼可见，
    # 而不是等用户反馈「列表怎么变慢了」才发现
    cache: str = "off"


class ClearHistoryResponse(BaseModel):
    message: str
    session_id: str


class UploadResponse(BaseModel):
    filename: str
    path: str
