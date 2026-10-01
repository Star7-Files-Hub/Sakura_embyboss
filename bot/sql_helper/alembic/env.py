import os

# B-L14 收尾：这一行必须位于任何 `import bot.sql_helper` 之前。
# 从 CLI 执行 `alembic upgrade head` 时，alembic 会加载本文件，而下面会 import bot.sql_helper；
# 该模块底部在导入期就调用了 run_migrations()，于是"外层 upgrade 里再嵌套执行一次完整 upgrade"
# —— 这正是 B-L14 所说的"依赖半初始化模块的部分导入"结构。
# 提前打标记，让 sql_helper 的导入期自动迁移让路：真正的迁移由本文件末尾的
# run_migrations_online()/run_migrations_offline() 执行。
os.environ["SAKURA_SKIP_AUTO_MIGRATE"] = "1"

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from bot.sql_helper import Base
from bot.sql_helper import sql_code, sql_emby, sql_emby2, sql_favorites, sql_partition, sql_request_record  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
