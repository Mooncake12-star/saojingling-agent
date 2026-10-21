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


def _install_sqlite_pragmas(target_engine: Engine) -> None:
    """为**某一个** Engine 的每条新连接设置 PRAGMA。

    必须按 engine 装，不能只在模块级 engine 上装：
        build_engine() 还会被测试、 alembic、将来的多库场景调用，
        如果 PRAGMA 只挂在模块级 engine 上，那些 engine 上的
        ON DELETE CASCADE 会静默失效——不报错，只是孤儿行留下来。
        （这个坑是在写知识库级联删除测试时踩到的。）

    放在 connect 事件里而不是建库时执行一次，是因为 WAL 是「按数据库」生效，
    而 foreign_keys 是「按连接」生效——每个新连接都要重新打开。
    """
    if target_engine.dialect.name != "sqlite":
        return

    @event.listens_for(target_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            # busy_timeout：写锁被占时先等 5 秒再报 database is locked，
            # 而不是立刻失败。并发写（每轮对话都要落两条消息）时，
            # 这一条决定的是「慢一点」还是「直接 500」
            cursor.execute("PRAGMA busy_timeout=5000")
            # journal_mode=WAL 本来是**数据库级**的持久设置，设一次永久生效，
            # 之后每条新连接执行它只是幂等确认。
            #
            # 但「确认」这个动作本身要拿一次短暂的独占锁。100 并发建连时抢不到，
            # pysqlite 会抛 sqlite3.OperationalError: unable to open database file
            # ——报错文字有误导性，实际是「这次确认没抢到锁」，不是库文件打不开。
            # 压测实测：100 并发打 /knowledge-bases，300 个请求 280 个 500，
            # 全挂在建连接的第一条 PRAGMA 上。
            #
            # 所以这里必须吞掉：库早就是 WAL 了，确认失败不影响正确性；
            # 真需要切模式的那一次发生在启动阶段（没有并发），不会走到这里。
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
        finally:
            cursor.close()


def build_engine(url: str | None = None) -> Engine:
    """按 URL 创建 Engine，SQLite 会自动补上必需的连接参数与 PRAGMA。"""
    url = url or get_database_url()
    connect_args: dict[str, object] = {}
    kwargs: dict[str, object] = {}
    if url.startswith("sqlite"):
        # FastAPI 的同步依赖可能在不同线程取连接，sqlite3 默认禁止跨线程复用
        connect_args["check_same_thread"] = False
        # 文件库默认是 QueuePool(5 + 10)，也就是最多 15 条连接。
        # anyio 的工作线程默认 40 个，100 并发时后 85 个请求要排队等连接，
        # 等满 pool_timeout=30 秒才报错——用户看到的就是「卡死然后 500」。
        # WAL 下读读、读写都不互斥，只有写写互斥，所以连接可以开多：
        # 20 + 20 = 40，正好等于线程池上限，不会出现「有线程但拿不到连接」。
        # 内存库（测试用）不吃连接数，保持默认。
        if ":memory:" not in url:
            kwargs.update(
                pool_size=20,
                max_overflow=20,
                # 等 10 秒还拿不到连接就报错，而不是让请求挂 30 秒
                pool_timeout=10,
            )
    elif url.startswith("postgresql"):
        # PostgreSQL 下连接不是「越多越好」：每条占用服务端一个 backend 进程，
        # max_connections 默认 100，多副本共享。
        # 10+10=20 条/副本，2 副本 40 条，留足余量。
        #
        # pool_recycle：PG 侧可能因超时/重启断掉空闲连接，
        # 定期回收避免拿到「看起来活着但已被服务端关闭」的 socket
        # （pool_pre_ping 也能兜住，但每次 checkout 都多一次往返）
        kwargs.update(
            pool_size=10,
            max_overflow=10,
            pool_timeout=10,
            pool_recycle=1800,
        )
    new_engine = create_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        connect_args=connect_args,
        **kwargs,
    )
    _install_sqlite_pragmas(new_engine)
    return new_engine


engine = build_engine()


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
