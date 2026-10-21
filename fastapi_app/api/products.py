"""产品只读接口。

这是本次分层新增的第一个「纯业务」接口：
路由（api）→ 仓储（repositories）→ 模型（models），
响应体由 schemas.ProductRead 决定，不直接抛出 ORM 对象。

为什么要加这个接口：原来产品数据只被 MCP 工具内部消费，HTTP 侧完全看不到。
加了它之后，前端可以做「产品列表」页，也让 P5 的 React 有第一个真实数据源。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from db.session import get_db
from schemas import ProductRead
from repositories.product import ProductRepository

router = APIRouter(tags=["产品"])


@router.get("/products", response_model=list[ProductRead], summary="列出产品")
def list_products(
    brand: str | None = Query(default=None, description="按品牌过滤"),
    limit: int = Query(default=50, ge=1, le=200, description="最多返回条数"),
    db: Session = Depends(get_db),
) -> list[ProductRead]:
    """同步路由：SQLite / psycopg 的驱动都是阻塞的，交给 FastAPI 的线程池跑。

    写成 async def 会让阻塞调用占住事件循环，单个慢查询就能拖住所有请求。
    """
    repo = ProductRepository(db)
    rows = repo.list_by_brand(brand) if brand else repo.list_all()
    return [ProductRead.model_validate(row) for row in rows[:limit]]


@router.get("/products/{product_id}", response_model=ProductRead, summary="产品详情")
def get_product(product_id: str, db: Session = Depends(get_db)) -> ProductRead:
    row = ProductRepository(db).get(product_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="产品不存在")
    return ProductRead.model_validate(row)
