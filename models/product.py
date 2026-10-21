"""产品表的 ORM 模型。

字段与旧库 data/zst.db 的 products 表一一对应，刻意不做重命名：
改名意味着导入历史数据时要多写一层映射，收益为零。

specs / features / suitable_for 三列在库里存的是 JSON 字符串（历史原因），
这里保留 Text 类型，另外提供只读属性做反序列化，避免每个调用方各写一遍 json.loads。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Product(Base):
    """扫地机器人产品。"""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    brand: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str | None] = mapped_column(String(64))
    price: Mapped[float | None] = mapped_column(Float)
    specs: Mapped[str | None] = mapped_column(Text, comment="JSON 对象字符串")
    features: Mapped[str | None] = mapped_column(Text, comment="JSON 数组字符串")
    suitable_for: Mapped[str | None] = mapped_column(Text, comment="JSON 数组字符串")
    rating: Mapped[float | None] = mapped_column(Float)

    @staticmethod
    def _loads(raw: str | None, default: Any) -> Any:
        """解析 JSON 字段。

        库里存在 NULL 或历史脏数据时不能让整个查询失败，
        所以解析失败就退回默认值，只影响展示，不影响取数。
        """
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    @property
    def specs_dict(self) -> dict[str, Any]:
        return self._loads(self.specs, {})

    @property
    def features_list(self) -> list[str]:
        return self._loads(self.features, [])

    @property
    def suitable_for_list(self) -> list[str]:
        return self._loads(self.suitable_for, [])

    # 原本这里有个 to_dict()：P1 收尾时删掉了。
    # 序列化是对外契约的一部分，交给 schemas/product.py 的 ProductRead 统一决定，
    # 否则「MCP 工具的输出」和「HTTP 接口的输出」会各写一套，慢慢就不一致了。

    def __repr__(self) -> str:
        return f"<Product id={self.id!r} brand={self.brand!r} name={self.name!r}>"
