"""
初始化数据库
"""
import os
import importlib
from pathlib import Path
from urllib.parse import quote

from bot import db_host, db_user, db_pwd, db_name, db_port
from bot import LOGGER
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker

# 创建engine对象
# B-L13：凭据必须 URL 转义（密码含 @ : / ? # 空格等会破坏 DSN 解析）。
# 这里用 quote(safe='')：它把空格编码为 %20，无论 SQLAlchemy 用 unquote 还是 unquote_plus
# 解析都能正确还原（quote_plus 会把空格编码成 '+'，在 unquote 下会变成字面加号）。
DATABASE_URL = (
    f"mysql+pymysql://{quote(str(db_user), safe='')}:{quote(str(db_pwd), safe='')}"
    f"@{db_host}:{db_port}/{db_name}?charset=utf8mb4"
)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    echo_pool=False,
    pool_size=16,
    pool_recycle=60 * 30,
    # B-L13：MySQL 侧 KILL/重启后，连接池里的死连接会在下一次请求时报 OperationalError
    pool_pre_ping=True,
    connect_args={"init_command": "SET NAMES utf8mb4"},
)

# 创建Base对象
Base = declarative_base()
Base.metadata.bind = engine

_MIGRATION_GUARD_ENV = "SAKURA_RUNNING_MIGRATIONS"
# B-L14 收尾：由 alembic/env.py 在 import bot.sql_helper 之前设置，
# 表示"当前是 alembic CLI 在驱动迁移"，此时导入期的自动迁移必须让路（见 run_migrations 注释）。
_MIGRATION_SKIP_ENV = "SAKURA_SKIP_AUTO_MIGRATE"
# B-L14 收尾：进程内幂等标记，保证重复导入/重复调用只真正执行一次迁移。
_MIGRATIONS_DONE = False


def _import_all_models():
    """导入全部 ORM 模型，保证 Base.metadata 完整"""
    from bot.sql_helper import sql_code, sql_emby, sql_emby2, sql_favorites, sql_partition, sql_request_record  # noqa: F401


def _legacy_create_all_tables():
    """
    在未安装 Alembic 或配置缺失时兜底建表，保证服务可启动。
    """
    _import_all_models()

    Base.metadata.create_all(bind=engine, checkfirst=True)


def check_schema_consistency():
    """
    B-C1 自检：比对 Base.metadata 与真实库表结构，发现缺失的列/表时告警。

    只记录日志，绝不抛异常中断启动（数据库不可达时静默跳过）。
    `CREATE TABLE IF NOT EXISTS` 不会纠正已存在表的列差异，这是 B-C1 能长期潜伏的根因，
    因此在迁移之后加一道轻量的结构漂移检查。
    """
    try:
        from sqlalchemy import inspect as sa_inspect

        _import_all_models()

        inspector = sa_inspect(engine)
        existing_tables = set(inspector.get_table_names())
        missing = []
        for table_name, table in Base.metadata.tables.items():
            if table_name not in existing_tables:
                missing.append(f"{table_name}(整表缺失)")
                continue
            db_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name not in db_columns:
                    missing.append(f"{table_name}.{column.name}")
        if missing:
            LOGGER.warning(
                "数据库结构与 ORM 模型不一致，以下列/表缺失，相关查询会报 Unknown column: "
                + ", ".join(missing)
            )
        else:
            LOGGER.info("数据库结构与 ORM 模型一致性自检通过")
    except Exception as e:
        LOGGER.warning(f"跳过数据库结构与 ORM 一致性自检: {e}")


def run_migrations():
    """
    启动时自动执行数据库迁移到最新版本。

    B-L14 收尾：保留"导入期调用"（任何入口 import sql_helper 时 schema 一定就绪，
    移出导入期会让 schema 就绪性变差），改用三重守卫消除重复执行与嵌套 upgrade：
    1. `_MIGRATIONS_DONE`：进程内幂等，重复导入/重复调用只跑一次；
    2. `_MIGRATION_SKIP_ENV`（SAKURA_SKIP_AUTO_MIGRATE）：由 alembic/env.py 在 import
       bot.sql_helper 之前设置，表示当前由 alembic CLI 驱动迁移 —— env.py 会 import 本模块，
       而本模块底部又会调用本函数，不跳过就会在外层 upgrade 里嵌套一次完整 upgrade；
    3. `_MIGRATION_GUARD_ENV`（SAKURA_RUNNING_MIGRATIONS）：保留原有语义，是本函数自己
       在触发 upgrade 期间设置/清理的重入保护（与 2 的区别：2 由外部 CLI 场景设置且不清理，
       只表示"别在导入期自动迁移"；3 只覆盖本函数 upgrade 的执行窗口）。
    """
    global _MIGRATIONS_DONE
    if _MIGRATIONS_DONE:
        return
    if os.getenv(_MIGRATION_SKIP_ENV) == "1":
        LOGGER.info("检测到 Alembic CLI 正在驱动迁移，跳过 sql_helper 导入期的自动迁移")
        return
    if os.getenv(_MIGRATION_GUARD_ENV) == "1":
        return

    try:
        alembic_command = importlib.import_module("alembic.command")
        alembic_config = importlib.import_module("alembic.config")
    except ImportError:
        LOGGER.warning("未安装 alembic，跳过自动迁移")
        _legacy_create_all_tables()
        check_schema_consistency()
        _MIGRATIONS_DONE = True
        return

    alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    if not alembic_ini.exists():
        LOGGER.warning(f"未找到 Alembic 配置文件，跳过自动迁移: {alembic_ini}")
        _legacy_create_all_tables()
        check_schema_consistency()
        _MIGRATIONS_DONE = True
        return

    os.environ[_MIGRATION_GUARD_ENV] = "1"
    try:
        Config = getattr(alembic_config, "Config")
        config = Config(str(alembic_ini))
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
        alembic_command.upgrade(config, "head")
        LOGGER.info("数据库迁移完成，当前已升级到最新版本")
        check_schema_consistency()
        _MIGRATIONS_DONE = True
    except Exception as e:
        LOGGER.error(f"数据库自动迁移失败: {e}")
        raise
    finally:
        os.environ.pop(_MIGRATION_GUARD_ENV, None)


# 调用sql_start()函数，返回一个Session工厂
def sql_start() -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


Session = sql_start()


run_migrations()
