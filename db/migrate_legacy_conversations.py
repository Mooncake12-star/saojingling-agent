"""把老库 data/zst.db 的会话历史一次性搬到新库 data/app.db。

为什么需要这个脚本：
    迁移只建表结构，不搬数据。切换数据源后，老库里已有的对话不会自动过去，
    用户打开界面会看到「历史全部消失」。

为什么必须保留原 id：
    `conversation_summary.folded_until_id` 记录的是 `conversations.id`。
    如果重新编号，摘要说的「前 20 条已折叠」就会指向错误的行，
    加载历史时会重复喂给模型或漏掉。

幂等：
    conversations 按主键 DO NOTHING（重复跑不会产生重复行，也不会覆盖新消息）；
    conversation_summary 按 session_id 覆盖（摘要总是以老库最后状态为准）。

用法（先迁移表结构，再搬数据）：
    alembic upgrade head
    D:/anaconda3/envs/langchain_env/python.exe -m db.migrate_legacy_conversations
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.session import session_scope
from models.conversation import Conversation, ConversationSummary
from utils.path_tool import get_abs_path

LEGACY_DB = "data/zst.db"


def _parse_dt(value) -> datetime | None:
    """老库的时间是 TEXT（'2026-10-20 13:04:33'），DateTime 列只接受 datetime。

    解析失败返回 None，交给 server_default 填当前时间，不让一条脏数据阻断整个迁移。
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def migrate(legacy_path: str | None = None) -> tuple[int, int]:
    """返回 (写入的消息数, 写入的摘要数)。已存在的记录计为 0。"""
    legacy_path = legacy_path or get_abs_path(LEGACY_DB)

    src = sqlite3.connect(legacy_path)
    src.row_factory = sqlite3.Row
    try:
        conversations = src.execute(
            "SELECT id, session_id, role, content, created_at FROM conversations ORDER BY id"
        ).fetchall()
        summaries = src.execute(
            "SELECT session_id, summary, folded_until_id, updated_at FROM conversation_summary"
        ).fetchall()
    finally:
        src.close()

    inserted_msgs = inserted_sums = 0

    with session_scope() as session:
        for r in conversations:
            values = {
                "id": r["id"],
                "session_id": r["session_id"],
                "role": r["role"],
                "content": r["content"],
            }
            created_at = _parse_dt(r["created_at"])
            if created_at is not None:
                values["created_at"] = created_at

            stmt = (
                sqlite_insert(Conversation)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            inserted_msgs += session.execute(stmt).rowcount or 0

        for r in summaries:
            values = {
                "session_id": r["session_id"],
                "summary": r["summary"],
                "folded_until_id": r["folded_until_id"] or 0,
            }
            updated_at = _parse_dt(r["updated_at"])
            if updated_at is not None:
                values["updated_at"] = updated_at

            stmt = sqlite_insert(ConversationSummary).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["session_id"],
                set_={
                    "summary": stmt.excluded.summary,
                    "folded_until_id": stmt.excluded.folded_until_id,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            session.execute(stmt)
            inserted_sums += 1

    return inserted_msgs, inserted_sums


def main() -> int:
    msgs, sums = migrate()
    print(f"[migrate] 写入消息 {msgs} 条，摘要 {sums} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
