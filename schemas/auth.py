"""鉴权相关的对外契约。

两点注意：

1. `UserRead` 里**没有** password_hash，也没有 token_version。
   响应模型不是「表的映射」，是「承诺给前端什么」。哈希值一旦出现在响应里，
   就会被浏览器、日志、代理缓存各留一份。

2. email 用正则校验而不是 pydantic 的 EmailStr。
   EmailStr 需要额外装 email-validator，而这里只需要挡住明显不是邮箱的输入；
   真正的有效性只能靠发验证邮件确认。
"""
from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    email: str = Field(max_length=255, description="登录邮箱，不区分大小写")
    password: str = Field(min_length=8, max_length=128, description="至少 8 位")
    # 公网部署时用 ZST_INVITE_CODE 开启校验；没配这个环境变量时字段填不填都行。
    # 设成可选而不是必填：本地开发、测试、离线演示都不需要邀请码，
    # 强制必填会让「clone 下来就能跑」这件事多一道无关的门槛。
    invite_code: str | None = Field(
        default=None, max_length=128, description="邀请码（仅当服务端开启校验时需要）"
    )

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("邮箱格式不正确")
        return value


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str
    is_active: bool
    created_at: datetime | None = None


class LogoutResponse(BaseModel):
    message: str
    token_version: int
