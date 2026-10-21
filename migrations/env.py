"""Alembic 运行环境。

两个关键点，缺任何一个都会让迁移工具失效：
    1. target_metadata 必须指向 Base.metadata，且模型必须被 import 过；
       否则 --autogenerate 会生成空迁移（不报错，最难发现）。
    2. SQLite 不支持大部分 ALTER TABLE，必须开 render_as_batch，
       否则以后改字段类型/加约束时生成的脚本在 SQLite 上直接报错。

数据库 URL 不写在 alembic.ini 里，统一从 db.session.get_database_url() 取，
避免两处配置不一致（这是「本地跑得好、线上连错库」的常见来源）。
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from alembic import context

# 保证无论从哪个目录执行 alembic，都能 import 到项目内的模块
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from db.base import Base  # noqa: E402
from db.session import get_database_url  # noqa: E402
import models  # noqa: E402,F401  必须导入，让所有模型注册到 Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 用代码里的配置覆盖 ini，保证迁移和运行时连的是同一个库
config.set_main_option("sqlalchemy.url", get_database_url())

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return get_database_url().startswith("sqlite")


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库（用于审查或 DBA 手工执行）。"""
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=_is_sqlite(),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,          # 字段类型变化也生成迁移
        compare_server_default=True,
        render_as_batch=_is_sqlite(),  # SQLite 改表的唯一可行方式
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连真实数据库执行。"""
    connectable = create_engine(
        get_database_url(),
        poolclass=pool.NullPool,   # 迁移是一次性动作，用连接池没意义
    )
    with connectable.connect() as connection:
        _run(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
