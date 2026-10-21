"""SQLAlchemy 声明式基类。

所有 ORM 模型都继承 Base，Base.metadata 汇总了全部表结构，
Alembic 就是读这个 metadata 来和数据库做 diff、生成迁移脚本的。
"""
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 约束命名规范：让索引/唯一约束/外键在数据库里有确定的名字。
# 不写的话 SQLite 会给出随机或匿名名字，后面想 drop 一个约束时无从下手。
# 这份字典必须在第一次生成迁移之前定下来，事后再改会导致迁移无法对齐。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
