"""会话记忆的数据访问。

对应 `agent/react_agent.py` 里原来的四段裸 SQL：
    _load_history()      → load_window()
    _save_message()      → append()
    _fold_old_messages() → upsert_summary()
    clear_history()      → clear()

P2 之后每个方法都必须带 user_id，且是**必填位置参数**而不是可选参数。
    这是刻意的：可选参数意味着「有人可以不传」，而漏传一次就是数据泄露。
    写成必填，忘记传会直接 TypeError，在开发期就暴露，不会留到线上去。

为什么把 UPSERT 也放进这一层：
    原来的 `INSERT ... ON CONFLICT(session_id) DO UPDATE` 是 SQLite 方言语法，
    写在 agent 里意味着「换数据库要改业务代码」。放在这里，将来换 PostgreSQL
    只改本文件的方言 import，agent 一行不动。

本层不提交事务：commit 由调用方（session_scope）决定，
这样「写消息 + 写摘要」可以放在同一个事务里，中途失败一起回滚。
"""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from models.conversation import Conversation, ConversationSummary


def _sqlite_insert(model):
    """集中一处引用 SQLite 方言，方便将来换库。"""
    return sqlite_insert(model)


class ConversationRepository:
    """某个用户的某个会话的消息与摘要。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def load_window(
        self, user_id: int, session_id: str
    ) -> tuple[str, int, list[tuple[int, str, str]]]:
        """一次取回「摘要 + 未折叠的消息」。

        返回 (summary, folded_until_id, [(id, role, content), ...])。
        只取 id > folded_until_id 的部分：更早的内容已经在摘要里，
        再取一遍等于把同一段对话喂给模型两次。
        """
        # 复合主键的 get 要传元组，顺序与模型里的字段声明一致
        row = self.session.get(ConversationSummary, (user_id, session_id))
        summary = (row.summary or "") if row else ""
        folded_until_id = (row.folded_until_id or 0) if row else 0

        # 只查三列而不是整个实体：这条路径每轮对话都会走，
        # 拿回用不上的 created_at 只是多一次反序列化。
        stmt = (
            select(Conversation.id, Conversation.role, Conversation.content)
            .where(
                Conversation.user_id == user_id,
                Conversation.session_id == session_id,
                Conversation.id > folded_until_id,
            )
            .order_by(Conversation.id)
        )
        rows = [(r.id, r.role, r.content) for r in self.session.execute(stmt)]
        return summary, folded_until_id, rows

    def append(self, user_id: int, session_id: str, role: str, content: str) -> int:
        """追加一条消息，返回新 id。

        flush 而不是 commit：需要立刻拿到自增 id（调用方要把它拼进内存窗口），
        但事务边界留给调用方。flush 只发 INSERT，commit 才落盘。
        """
        msg = Conversation(user_id=user_id, session_id=session_id, role=role, content=content)
        self.session.add(msg)
        self.session.flush()
        return msg.id

    def upsert_summary(
        self, user_id: int, session_id: str, summary: str, folded_until_id: int
    ) -> None:
        """写入或更新摘要。

        ON CONFLICT 是原子操作：先 SELECT 再决定 INSERT/UPDATE 的写法，
        在两个请求同时折叠同一个会话时会插入冲突（或产生两行）。
        """
        stmt = _sqlite_insert(ConversationSummary).values(
            user_id=user_id,
            session_id=session_id,
            summary=summary,
            folded_until_id=folded_until_id,
        )
        stmt = stmt.on_conflict_do_update(
            # 冲突目标是复合主键：同一个用户 + 同一个会话才算同一个摘要
            index_elements=[ConversationSummary.user_id, ConversationSummary.session_id],
            set_={
                "summary": stmt.excluded.summary,
                "folded_until_id": stmt.excluded.folded_until_id,
                # 这里必须显式调数据库函数：excluded.updated_at 在没显式插入该列时是 NULL，
                # server_default 只在 INSERT 生效，不会进入 excluded。
                "updated_at": func.current_timestamp(),
            },
        )
        self.session.execute(stmt)

    def clear(self, user_id: int, session_id: str) -> int:
        """删除该会话的全部消息与摘要，返回删除的消息条数。

        conversations 与 summary 之间没有外键，不能指望级联删除，必须显式删两次。
        """
        deleted = self.session.execute(
            delete(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.session_id == session_id,
            )
        ).rowcount
        self.session.execute(
            delete(ConversationSummary).where(
                ConversationSummary.user_id == user_id,
                ConversationSummary.session_id == session_id,
            )
        )
        return deleted

    def count_messages(self, user_id: int, session_id: str) -> int:
        """该会话的消息总数（不含摘要），供测试与统计使用。"""
        stmt = select(Conversation.id).where(
            Conversation.user_id == user_id,
            Conversation.session_id == session_id,
        )
        return len(self.session.scalars(stmt).all())

    def list_sessions(self, user_id: int) -> list[tuple[str, int]]:
        """该用户的所有会话及其消息数，按最近活跃排序。

        给「会话列表」页面用；也顺手证明了「查不到别人的会话」——
        整个方法里没有一处可以传别人的 user_id。
        """
        stmt = (
            select(
                Conversation.session_id,
                func.count(Conversation.id),
                func.max(Conversation.id),
            )
            .where(Conversation.user_id == user_id)
            .group_by(Conversation.session_id)
            .order_by(func.max(Conversation.id).desc())
        )
        return [(row[0], row[1]) for row in self.session.execute(stmt)]
