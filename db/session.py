"""Engine 与 Session 的统一入口。

本模块负责三件事：
    1. 创建 Engine（数据库连接池）
    2. 提供 Session 工厂 SessionLocal
    3. 提供两种使用方式：FastAPI 依赖 get_db / 脚本用 session_scope

与 db/database.py 的关系：
    两者暂时并存，互不影响。database.py 是旧的裸 sqlite3 实现，仍被 agent 使用
    并指向 data/zst.db；本模块是新的 SQLAlchemy 实现，指向 data/app.db。
    等 P1 的调用点全部迁移完成后再删除 database.py。

连接 URL 可通过环境变量 ZST_DB_URL 覆盖，测试和将来切 PostgreSQL 都靠它。
"""
from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from utils.path_tool import get_abs_path

DEFAULT_DB_FILE = "data/app.db"


def get_database_url() -> str:
    """返回当前使用的数据库 URL。

    优先级：环境变量 ZST_DB_URL > 默认的 data/app.db。
    默认值拼成绝对路径，避免从不同工作目录启动时连到不同的库文件。
    """
    env_url = os.getenv("ZST_DB_URL", "").strip()
    if env_url:
        return env_url
    return f"sqlite:///{get_abs_path(DEFAULT_DB_FILE)}"


def build_engine(url: str | None = None) -> Engine:
    """按 URL 创建 Engine，SQLite 会自动补上必需的连接参数。"""
    url = url or get_database_url()
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # FastAPI 的同步依赖可能在不同线程取连接，sqlite3 默认禁止跨线程复用
        connect_args["check_same_thread"] = False
    return create_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


engine = build_engine()


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    """每条新连接建立时统一设置 PRAGMA。

    放在事件里而不是建库时执行一次，是因为 WAL 是「按数据库」生效，
    而 foreign_keys 是「按连接」生效——每个新连接都要重新打开。
    """
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


# expire_on_commit=False：commit 之后对象仍可读，否则 FastAPI 序列化响应时
# 会触发一次隐式刷新，Session 已关闭就会抛 DetachedInstanceError。
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个 Session，请求结束自动关闭。

    用法：
        def endpoint(db: Session = Depends(get_db)): ...

    Session 必须由框架统一释放：异常路径下如果只写 db.close() 而不 rollback，
    未提交的事务会挂在那里占着连接，连接池很快耗尽。
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """脚本 / 后台任务用的事务上下文：正常提交，异常回滚，始终关闭。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
