"""把 data/products.json 导入 products 表。

用途：迁移只建表结构，不搬数据。新库 data/app.db 建好后跑一次这个脚本，
后续写 repository 时就有真实数据可用。

幂等：按主键 upsert，重复执行不会产生重复行，也不会清空表。

用法（必须用项目自己的虚拟环境）：
    D:/anaconda3/envs/langchain_env/python.exe -m db.seed_products
"""
from __future__ import annotations

import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.session import SessionLocal
from models.product import Product
from utils.path_tool import get_abs_path


def _as_json_text(value) -> str | None:
    """把 dict / list 序列化成库里的 TEXT 形式，已经是字符串就原样返回。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def seed_products(session: Session, json_path: str | None = None) -> tuple[int, int]:
    """导入产品数据，返回 (新增数, 更新数)。"""
    json_path = json_path or get_abs_path("data/products.json")
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"产品数据文件不存在：{json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        products = json.load(f)

    # 一次查出已存在的 id，避免在循环里逐条 SELECT（那是最典型的 N+1）
    existing_ids = set(session.scalars(select(Product.id)).all())

    created = updated = 0
    for item in products:
        pid = item["id"]
        row = session.get(Product, pid) if pid in existing_ids else None
        is_new = row is None
        if is_new:
            row = Product(id=pid)
            session.add(row)

        row.brand = item.get("brand")
        row.name = item.get("name") or ""
        row.type = item.get("type")
        row.price = item.get("price")
        row.specs = _as_json_text(item.get("specs"))
        row.features = _as_json_text(item.get("features"))
        row.suitable_for = _as_json_text(item.get("suitable_for"))
        row.rating = item.get("rating")

        if is_new:
            created += 1
        else:
            updated += 1

    return created, updated


def main() -> int:
    with SessionLocal() as session:
        created, updated = seed_products(session)
        session.commit()
        total = len(session.scalars(select(Product.id)).all())
    print(f"[seed] 新增 {created} 条，更新 {updated} 条，当前共 {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
