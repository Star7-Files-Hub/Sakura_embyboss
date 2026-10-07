"""add kick_until to emby

Revision ID: 20260315_04
Revises: 20260315_03
Create Date: 2026-10-06 11:10:00

配合「临时封禁踢流」：`bot/sql_helper/sql_emby.py` 的 ORM 模型 `Emby` 新增
`kick_until` 列，用于记录「机器人把这个用户的 Emby 账号临时禁用、到期需还原」
的到期时间。

为什么需要它：这台 Emby 4.10 上 `POST /Sessions/{Id}/Playing/Stop` 对现有客户端
群体 100% 无效（实测 `SupportsRemoteControl=True` 的会话 0/3643），唯一由服务器
自己执行、真正能停流的杠杆是把用户策略的 `IsDisabled` 置真。但置真与还原之间隔着
几十秒（实测客户端 5~67 秒才放弃），进程若在此期间异常退出，用户会被永久锁死。
把到期时间落库后，启动时可以扫出来还原。

与 20260315_03 同样的幂等写法：按 `inspect` 结果判断，列已存在则跳过。
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260315_04"
down_revision = "20260315_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "emby" not in inspector.get_table_names():
        # 理论上 20260315_01 已建表；表不存在时不静默跳过，交给后续迁移/兜底建表处理
        return
    column_names = {column["name"] for column in inspector.get_columns("emby")}
    if "kick_until" not in column_names:
        # nullable=True 且无 server_default：NULL 就是「没有待还原的临时封禁」，
        # 存量行天然为 NULL，语义正确，不需要回填。
        op.add_column("emby", sa.Column("kick_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "emby" not in inspector.get_table_names():
        return
    column_names = {column["name"] for column in inspector.get_columns("emby")}
    if "kick_until" in column_names:
        op.drop_column("emby", "kick_until")
