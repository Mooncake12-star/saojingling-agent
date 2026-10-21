"""add users and user_id to conversations

Revision ID: 69263fb30b8a
Revises: 2cbce6ce7786
Create Date: 2026-10-20 22:03:51.673805

P2：给所有历史数据找到主人。

这个脚本不是 autogenerate 直接产出的，手工改了三处，原因写在这里：

1. autogenerate 给 `user_id` 生成的是 `nullable=False`，但表里已经有 66 行数据。
   SQLite 的 `ALTER TABLE ADD COLUMN NOT NULL` 在不给默认值时会直接报错
   （Cannot add a NOT NULL column with default value NULL）。
   所以改成三步：先加可空列 → 回填 → 再改成 NOT NULL。

2. autogenerate **看不到**主键变更。conversation_summary 的主键要从
   (session_id) 变成 (user_id, session_id) —— 不改的话，两个用户都用 "default"
   会互相覆盖摘要，而且不报错。SQLite 不支持 ALTER PRIMARY KEY，
   只能建新表 + 搬数据 + 删旧表 + 改名，也就是常说的「12 步改表」。

3. 需要先插入 legacy 账号（id=1），否则回填时外键无处可指。
   这个 id 与 models/user.py 的 LEGACY_USER_ID 必须一致，写死不能改。

关于 legacy 账号：
    is_active=0、password_hash='!disabled'，它是一个「数据接收人」，不是可登录账号。
    verify_password 遇到非法哈希会返回 False（内部捕获 ValueError），
    加上 is_active=0，两条路都走不通。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '69263fb30b8a'
down_revision: Union[str, Sequence[str], None] = '2cbce6ce7786'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_USER_ID = 1


def upgrade() -> None:
    """Upgrade schema."""
    # ---------- 1. 用户表 ----------
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=16), server_default=sa.text("'user'"), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('token_version', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

    # ---------- 2. 历史数据接盘人 ----------
    op.execute(
        sa.text(
            "INSERT INTO users (id, email, password_hash, role, is_active, token_version) "
            "VALUES (:id, :email, :pwd, 'user', 0, 0)"
        ).bindparams(id=LEGACY_USER_ID, email='legacy@local', pwd='!disabled')
    )

    # ---------- 3. conversations 加 user_id ----------
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))

    # 回填：P2 之前没有用户体系，全部历史消息归 legacy
    op.execute(sa.text("UPDATE conversations SET user_id = :uid").bindparams(uid=LEGACY_USER_ID))

    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=False)
        # 旧的单列索引不再匹配新查询（WHERE user_id=? AND session_id=?）
        batch_op.drop_index('ix_conversations_session_id')
        batch_op.create_index('ix_conversations_user_session', ['user_id', 'session_id'], unique=False)
        batch_op.create_foreign_key(
            op.f('fk_conversations_user_id_users'),
            'users', ['user_id'], ['id'], ondelete='CASCADE',
        )

    # ---------- 4. conversation_summary 换复合主键 ----------
    # 先加可空列，回填后再重建表（重建时一次性写成 NOT NULL）
    with op.batch_alter_table('conversation_summary', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))

    op.execute(
        sa.text("UPDATE conversation_summary SET user_id = :uid").bindparams(uid=LEGACY_USER_ID)
    )

    op.create_table(
        'conversation_summary_new',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('folded_until_id', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name=op.f('fk_conversation_summary_user_id_users'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('user_id', 'session_id', name=op.f('pk_conversation_summary')),
    )
    # 显式列出列名，不用 SELECT *：列顺序变了也不会搬错
    op.execute(
        "INSERT INTO conversation_summary_new (user_id, session_id, summary, folded_until_id, updated_at) "
        "SELECT user_id, session_id, summary, folded_until_id, updated_at FROM conversation_summary"
    )
    op.drop_table('conversation_summary')
    op.rename_table('conversation_summary_new', 'conversation_summary')


def downgrade() -> None:
    """Downgrade schema.

    注意：回滚会把 user_id 信息丢掉（旧结构里没有这一列），
    多用户的会话会全部混回同一个 session_id 命名空间。
    这正是「不可逆变更」的定义——回滚只保证结构能退回去，不保证数据还能分得清。
    """
    # ---------- 4. 摘要表退回单列主键 ----------
    op.create_table(
        'conversation_summary_old',
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('folded_until_id', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('session_id', name=op.f('pk_conversation_summary')),
    )
    op.execute(
        "INSERT OR REPLACE INTO conversation_summary_old (session_id, summary, folded_until_id, updated_at) "
        "SELECT session_id, summary, folded_until_id, updated_at FROM conversation_summary"
    )
    op.drop_table('conversation_summary')
    op.rename_table('conversation_summary_old', 'conversation_summary')

    # ---------- 3. conversations 去掉 user_id ----------
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_constraint(op.f('fk_conversations_user_id_users'), type_='foreignkey')
        batch_op.drop_index('ix_conversations_user_session')
        batch_op.create_index('ix_conversations_session_id', ['session_id'], unique=False)
        batch_op.drop_column('user_id')

    # ---------- 2. 用户表 ----------
    op.execute(sa.text("DELETE FROM users WHERE id = :uid").bindparams(uid=LEGACY_USER_ID))
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
