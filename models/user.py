"""用户表。

P2 引入的第一张「身份」表。几个设计决定值得记住：

1. 只存哈希，不存密码
   argon2 每次哈希都带随机 salt，同一个密码两次存进去的值也不同。
   校验是「用存下来的值里的 salt 再算一遍，比对结果」，不是解密。

2. email 唯一，且**不区分大小写**
   SQLite 的 UNIQUE 对 `A@x.com` 和 `a@x.com` 认为是两个值。
   所以入库前统一 `.lower()`（见 repositories/user.py），否则同一个人能注册两次。

3. token_version 是「踢下线」的开关
   JWT 是无状态的，签发后到过期前都有效，服务端没有东西可删。
   把版本号写进 token，校验时比对用户的当前版本：
   改密码 / 强制登出时把 version + 1，所有旧 token 立刻失效。

4. LEGACY_USER_ID = 1 是历史数据的归属人
   P2 之前所有会话都没有主，迁移时统一归到 id=1 的 legacy 账号。
   这个 id 是写死在迁移脚本里的，不能变——变了历史数据就认不回来了。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# 历史数据归属账号：P2 之前没有用户体系，那些会话必须有个人接着
LEGACY_USER_ID = 1
LEGACY_USER_EMAIL = "legacy@local"

ROLE_USER = "user"
ROLE_ADMIN = "admin"


class User(Base):
    """平台用户。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ROLE_USER, server_default=text("'user'")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    # 每次 +1 会让该用户已签发的全部 token 失效
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} role={self.role!r}>"
