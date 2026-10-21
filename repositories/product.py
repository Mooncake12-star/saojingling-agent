"""产品表的数据访问。

为什么要这一层：
    `agent/tools/mcp_server.py` 里原本有两处一模一样的 `SELECT * FROM products`
    （product_recommend 和 price_query 各一份）。这种重复的代价不是多写两行，
    而是**以后加一个条件必须改两个地方**：比如「下架产品不返回」，
    漏改一处就会在某个工具里露出已下架的产品。

    收敛到本文件后，调用方只表达意图（list_all / search_by_name），
    SQL 怎么写只有一处决定。

本层不做的事：
    - 不 commit：事务边界由调用方（session_scope / get_db）控制
    - 不打分不排序业务优先级：那是 ProductService 的事
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models.product import Product


def escape_like(value: str) -> str:
    """转义 LIKE 的通配符。

    不转义会出两个 bug：
        搜 "50%"  → 匹配到所有以 50 开头的名字
        搜 "_"    → 匹配到所有单字符的名字
    反斜杠必须最先替换，否则会把后面新加的转义符再转一次。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ProductRepository:
    """所有产品查询的唯一入口。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self) -> list[Product]:
        """全部产品。

        显式 order_by：原代码没写 ORDER BY，结果顺序由存储引擎决定，
        今天按插入顺序返回，明天重建表就可能变，前端展示会莫名其妙换顺序。
        """
        stmt = select(Product).order_by(Product.id)
        return list(self.session.scalars(stmt))

    def get(self, product_id: str) -> Product | None:
        """按主键取一条。session.get 会先查 identity map，命中就不发 SQL。"""
        return self.session.get(Product, product_id)

    def list_by_brand(self, brand: str) -> list[Product]:
        stmt = select(Product).where(Product.brand == brand).order_by(Product.id)
        return list(self.session.scalars(stmt))

    def search_by_name(self, keyword: str) -> list[Product]:
        """按名称或 id 模糊匹配，大小写不敏感。

        用 SQL 的 LIKE 过滤，而不是把全表取到 Python 里再筛：
        现在只有 6 条数据，两种写法没区别；数据涨到 10 万条时，
        前者只回传命中的几行，后者要把整张表搬进内存。

        大小写：SQLite 的 LIKE 默认只对 ASCII 大小写不敏感，中文没有大小写，
        所以 lower() 统一处理足够，不需要 ILIKE（SQLite 也不支持）。
        """
        kw = keyword.strip().lower()
        if not kw:
            return []

        pattern = f"%{escape_like(kw)}%"
        stmt = (
            select(Product)
            .where(
                or_(
                    func.lower(Product.name).like(pattern, escape="\\"),
                    func.lower(Product.id).like(pattern, escape="\\"),
                )
            )
            .order_by(Product.id)
        )
        return list(self.session.scalars(stmt))
