"""add concurrent_warn_count to emby

Revision ID: 20260315_03
Revises: 20260315_02
Create Date: 2026-03-15 14:00:00

修复 B-C1：`bot/sql_helper/sql_emby.py` 的 ORM 模型 `Emby` 声明了
`concurrent_warn_count` 列，但历史迁移的 `CREATE TABLE IF NOT EXISTS emby`
列清单中没有它，导致空库经 alembic 建表后所有 `SELECT ... FROM emby`
报 pymysql 1054 Unknown column。此处按 `inspect` 结果幂等补齐该列。
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260315_03"
down_revision = "20260315_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "emby" not in inspector.get_table_names():
        # 理论上 20260315_01 已建表；表不存在时不静默跳过，交给后续迁移/兜底建表处理
        return
    column_names = {column["name"] for column in inspector.get_columns("emby")}
    if "concurrent_warn_count" not in column_names:
        op.add_column(
            "emby",
            sa.Column("concurrent_warn_count", sa.Integer(), nullable=True, server_default="0"),
        )
        # 历史行可能为 NULL，统一回填 0，保证 `count + 1` 之类的算术不会踩到 None
        op.execute("UPDATE `emby` SET `concurrent_warn_count` = 0 WHERE `concurrent_warn_count` IS NULL")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "emby" not in inspector.get_table_names():
        return
    column_names = {column["name"] for column in inspector.get_columns("emby")}
    if "concurrent_warn_count" in column_names:
        op.drop_column("emby", "concurrent_warn_count")
