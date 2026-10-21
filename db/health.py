"""数据库就绪检查（服务启动时调用）。

为什么需要它：
    改造前，FastAPI 启动时调用一次 `get_connection()`，顺手把 `CREATE TABLE IF NOT EXISTS`
    跑一遍，相当于「自动建表」。表结构交给 Alembic 之后，这个自动建表必须去掉——
    它会让「忘了执行迁移」的环境看起来一切正常，直到第一次用到新字段才崩，
    而那时的报错是 `no such column`，离真正原因很远。

    改成显式检查：缺表或缺版本号，启动直接失败，并把修复命令打在错误信息里。
    这就是 fail fast —— 在启动时炸，比在用户请求时炸好一百倍。
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from db.session import engine

# 必须存在的表。漏一个就说明迁移没跑完。
REQUIRED_TABLES = ("products", "conversations", "conversation_summary")


def ensure_schema_ready() -> str:
    """检查表结构是否就绪，返回当前迁移版本号。

    异常：
        RuntimeError —— 表缺失或没有迁移记录，消息里带修复命令。
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    missing = [t for t in REQUIRED_TABLES if t not in existing]
    if missing:
        raise RuntimeError(
            f"数据库未初始化，缺少表: {', '.join(missing)}。"
            f"请在项目根执行：alembic upgrade head"
        )

    if "alembic_version" not in existing:
        raise RuntimeError("数据库没有迁移记录（缺 alembic_version 表）。请在项目根执行：alembic upgrade head")

    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()

    if row is None:
        raise RuntimeError("alembic_version 表为空，数据库未记录任何迁移版本。请执行：alembic upgrade head")

    return str(row[0])
