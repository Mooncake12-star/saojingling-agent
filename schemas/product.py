"""产品的对外形状。

这里做了一件 ORM 模型不该做的事：把库里存的 JSON 字符串解析成结构。
放在 schema 而不是 model 的理由——「存什么」和「对外给什么」是两个决定，
今后接口要加字段（比如图片 URL、库存状态）只需要改这个文件，
不用动表，也不用动 repositories。

解析失败一律退回空值：历史库里存在脏数据，宁可少给一个特征，
也不该因为一条数据让整个列表接口 500。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProductRead(BaseModel):
    """单个产品的响应体。"""

    # from_attributes：允许 model_validate(orm_obj) 直接读对象属性
    model_config = ConfigDict(from_attributes=True)

    id: str
    brand: str | None = None
    name: str
    type: str | None = None
    price: float | None = None
    rating: float | None = None

    # 库里是 TEXT（JSON 字符串），对外是结构化的；用 before 校验器兼容两种输入
    specs: dict[str, Any] = Field(default_factory=dict)
    features: list[str] = Field(default_factory=list)
    suitable_for: list[str] = Field(default_factory=list)

    @field_validator("specs", mode="before")
    @classmethod
    def _parse_specs(cls, value: Any) -> dict[str, Any]:
        parsed = _loads(value, {})
        return parsed if isinstance(parsed, dict) else {}

    @field_validator("features", "suitable_for", mode="before")
    @classmethod
    def _parse_list(cls, value: Any) -> list[str]:
        parsed = _loads(value, [])
        return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
