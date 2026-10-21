"""用户表的数据访问。

email 一律 `strip().lower()` 之后再查/存。
理由：SQLite 的 UNIQUE 是大小写敏感的，`A@x.com` 和 `a@x.com` 会被当成两个用户。
如果不统一，同一个人能注册两次，登录时又只能命中其中一个——这类 bug 很难在测试里撞到。
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models.user import ROLE_USER, User


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == normalize_email(email))
        return self.session.scalars(stmt).first()

    def create(self, email: str, password_hash: str, role: str = ROLE_USER) -> User:
        user = User(
            email=normalize_email(email),
            password_hash=password_hash,
            role=role,
        )
        self.session.add(user)
        self.session.flush()   # 拿到自增 id，事务边界仍由调用方决定
        return user

    def bump_token_version(self, user_id: int) -> int:
        """把 token 版本 +1，该用户已签发的所有 token 立即失效。

        用 SQL 的 `version = version + 1` 而不是「读出来 +1 再写回」：
        后者在并发下会丢掉其中一次自增（两个请求都读到 3，都写 4）。
        """
        self.session.execute(
            update(User).where(User.id == user_id).values(token_version=User.token_version + 1)
        )
        self.session.flush()
        user = self.session.get(User, user_id)
        return user.token_version if user else -1

    def list_all(self) -> list[User]:
        return list(self.session.scalars(select(User).order_by(User.id)))
