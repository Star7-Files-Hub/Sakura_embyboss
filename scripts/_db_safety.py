#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/ 下"会写数据库"的脚本共用的安全护栏（D-H5）。

背景：`reproduce_register_races.py` 与 `test_register_queue.py --real` 会直接读取仓库根目录
的 `config.json`（生产凭据）连接数据库，并对 `emby` / `Rcode` 表执行 DELETE / INSERT。
旧版本还存在两个放大风险：

1. 测试主键由 `int(time.time())` 生成（约 1.7e9，10 位），落在真实 Telegram 用户 ID 的
   取值区间内，DELETE 有真实概率命中真实用户记录；
2. 没有任何"这是测试库"的判断，也没有二次确认。

本模块提供：
- `TEST_ID_BASE`：远离 Telegram ID 取值域的负数段（TG 用户 ID 为正数，超级群/频道 ID
  约为 -1e12 量级，-1e15 段不会与任何真实 ID 冲突）；
- `assert_test_database_name()`：库名不符合测试约定时直接拒绝执行；
- `assert_expected_tables()`：确认连的是本项目的表结构；
- `describe_rows()`：删除前把将被删除的行打印出来，便于人工核对。

**禁止对生产库运行。**
"""
from typing import Iterable, List, Sequence

# 测试主键基数：-1e15，远离 Telegram ID 取值域（D-H5）
TEST_ID_BASE = -1_000_000_000_000_000

# 库名必须包含该子串才允许执行写操作（D-H5）
TEST_DB_NAME_HINT = "test"

# 本项目预期的表名，用于确认目标库不是"别的库"
EXPECTED_TABLES: Sequence[str] = ("emby", "Rcode")


def parse_db_name(database_url: str) -> str:
    """从 SQLAlchemy URL 中取出库名。"""
    try:
        from sqlalchemy.engine import make_url

        return make_url(database_url).database or ""
    except Exception:
        # 兜底：手工解析 .../<dbname>?params
        tail = database_url.rsplit("/", 1)[-1]
        return tail.split("?", 1)[0]


def assert_test_database_name(db_name: str) -> None:
    """生产库保护：库名不符合测试约定时拒绝执行（D-H5）。"""
    if TEST_DB_NAME_HINT not in (db_name or "").lower():
        raise SystemExit(
            "[拒绝执行] 目标库 {0!r} 看起来不是测试库。\n"
            "  本脚本会对 emby / Rcode 表执行 DELETE / INSERT，"
            "只允许连接库名包含 {1!r} 的独立测试库（例如 embyboss_test）。\n"
            "  请通过 --database-url 指定测试库，或把 config.json 的 db_name "
            "临时指向测试库。\n"
            "  **禁止对生产库运行。**".format(db_name, TEST_DB_NAME_HINT)
        )


def assert_expected_tables(engine) -> None:
    """确认目标库里存在本项目预期的表，避免误连到无关数据库（D-H5）。"""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    missing = [name for name in EXPECTED_TABLES if name not in existing]
    if missing:
        raise SystemExit(
            "[拒绝执行] 目标库缺少预期表 {0}（现有表：{1}）。\n"
            "  这通常意味着连接到了错误的数据库。".format(
                ", ".join(missing), ", ".join(sorted(existing)) or "<空>"
            )
        )


def describe_rows(rows: Iterable, key: str, label: str) -> List[str]:
    """把"即将被删除"的行格式化成可打印的列表，便于人工核对（D-H5）。"""
    lines: List[str] = []
    for row in rows:
        lines.append("  - {0} {1}={2} name={3}".format(label, key, getattr(row, key, None),
                                                       getattr(row, "name", None)))
    return lines
