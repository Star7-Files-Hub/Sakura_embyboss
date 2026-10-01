#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
注册码并发竞态复现脚本。

⚠️ **禁止对生产库运行**（D-H5）
    本脚本会连接数据库并对 `emby` / `Rcode` 表执行 DELETE / INSERT。
    为降低误删真实用户的风险，脚本现在要求：
      1. 目标库的库名必须包含 "test"（例如 `embyboss_test`），否则直接拒绝启动；
      2. 必须显式加上 `--i-know-what-i-am-doing` 才会真正执行写操作，
         不加该开关时等价于 `--dry-run`（只打印将会被删除的行）；
      3. 测试主键取自远离 Telegram ID 取值域的负数段
         （见 `scripts/_db_safety.TEST_ID_BASE`），不会再命中真实 TG ID。
    推荐用法（先建一个独立测试库，再用 --database-url 指过去）：
        python3 scripts/reproduce_register_races.py same-user-multi-code \
            --database-url 'mysql+pymysql://user:pwd@127.0.0.1:3306/embyboss_test?charset=utf8mb4' \
            --i-know-what-i-am-doing
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from _db_safety import (
    TEST_ID_BASE,
    assert_expected_tables,
    assert_test_database_name,
    describe_rows,
    parse_db_name,
)


def load_database_url(explicit: str = None) -> str:
    """默认沿用 config.json 的连接串；--database-url 可显式指向独立测试库。"""
    if explicit:
        return explicit
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        _config = json.load(f)

    return (
        f"mysql+pymysql://{_config['db_user']}:{_config['db_pwd']}"
        f"@{_config['db_host']}:{_config['db_port']}/{_config['db_name']}?charset=utf8mb4"
    )


# engine / Session 改为在 main() 里初始化（这样 --database-url 才能真正生效）
engine = None
Session = None
Base = declarative_base()


class Emby(Base):
    __tablename__ = "emby"

    tg = Column(BigInteger, primary_key=True, autoincrement=False)
    embyid = Column(String(255), nullable=True)
    name = Column(String(255), nullable=True)
    pwd = Column(String(255), nullable=True)
    pwd2 = Column(String(255), nullable=True)
    lv = Column(String(1), default="d")
    cr = Column(DateTime, nullable=True)
    ex = Column(DateTime, nullable=True)
    us = Column(Integer, default=0)
    iv = Column(Integer, default=0)
    ch = Column(DateTime, nullable=True)


class Code(Base):
    __tablename__ = "Rcode"

    code = Column(String(50), primary_key=True, autoincrement=False)
    tg = Column(BigInteger)
    us = Column(Integer)
    used = Column(BigInteger, nullable=True)
    usedtime = Column(DateTime, nullable=True)


# 测试主键：负数高段位，不会与真实 Telegram ID 冲突（D-H5）
_id_counter = count(TEST_ID_BASE)


@dataclass
class ScenarioContext:
    user_id: int
    issuer_id: int
    codes: list[str]


def next_id() -> int:
    return next(_id_counter)


def sql_get_emby(tg):
    with Session() as session:
        return session.query(Emby).filter(Emby.tg == tg).first()


@contextmanager
def seeded_context(code_count: int = 2, us: int = 30):
    user_id = next_id()
    issuer_id = next_id()
    codes = [f"race-{user_id}-{i}" for i in range(code_count)]

    with Session() as session:
        # 删除前先把将被删除的行打印出来，便于人工核对（D-H5）
        existing_emby = session.query(Emby).filter(Emby.tg.in_([user_id, issuer_id])).all()
        existing_codes = session.query(Code).filter(Code.code.in_(codes)).all()
        for line in describe_rows(existing_emby, "tg", "emby") + describe_rows(existing_codes, "code", "Rcode"):
            print(f"[cleanup] 即将删除{line}")

        session.query(Code).filter(Code.code.in_(codes)).delete(synchronize_session=False)
        session.query(Emby).filter(Emby.tg.in_([user_id, issuer_id])).delete(synchronize_session=False)
        session.add(Emby(tg=user_id, lv="d", us=0, iv=0))
        session.add(Emby(tg=issuer_id, lv="b", us=0, iv=0))
        session.add_all([Code(code=code, tg=issuer_id, us=us) for code in codes])
        session.commit()

    try:
        yield ScenarioContext(user_id=user_id, issuer_id=issuer_id, codes=codes)
    finally:
        with Session() as session:
            session.query(Code).filter(Code.code.in_(codes)).delete(synchronize_session=False)
            session.query(Emby).filter(Emby.tg.in_([user_id, issuer_id])).delete(synchronize_session=False)
            session.commit()


def fetch_user_state(user_id: int) -> dict:
    with Session() as session:
        user = session.query(Emby).filter(Emby.tg == user_id).first()
        return {
            "tg": user.tg,
            "embyid": user.embyid,
            "us": user.us,
            "lv": user.lv,
            "ex": user.ex,
        }


def fetch_code_state(code: str) -> dict:
    with Session() as session:
        row = session.query(Code).filter(Code.code == code).first()
        return {
            "code": row.code,
            "used": row.used,
            "usedtime": row.usedtime,
            "us": row.us,
        }


def old_redeem_register_code(user_id: int, register_code: str, delay: float = 0.2) -> dict:
    data = sql_get_emby(tg=user_id)
    if not data:
        return {"status": "no_user"}
    if data.embyid:
        return {"status": "has_account"}
    if int(data.us or 0) > 0:
        return {"status": "already_qualified"}

    time.sleep(delay)

    with Session() as session:
        code = session.query(Code).filter(Code.code == register_code).with_for_update().first()
        if not code:
            return {"status": "invalid_code"}
        if code.used is not None:
            return {"status": "used", "used": code.used}

        code.used = user_id
        code.usedtime = datetime.now()
        session.commit()

        new_us = int(data.us or 0) + int(code.us or 0)
        session.query(Emby).filter(Emby.tg == user_id).update({Emby.us: new_us})
        session.commit()
        return {"status": "ok", "new_us": new_us, "code": register_code}


def fixed_redeem_register_code(user_id: int, register_code: str, delay: float = 0.2) -> dict:
    time.sleep(delay)
    with Session() as session:
        user = session.query(Emby).filter(Emby.tg == user_id).with_for_update().first()
        if not user:
            return {"status": "no_user"}
        if user.embyid:
            return {"status": "has_account"}
        if int(user.us or 0) > 0:
            return {"status": "already_qualified"}

        code = session.query(Code).filter(Code.code == register_code).with_for_update().first()
        if not code:
            return {"status": "invalid_code"}
        if code.used is not None:
            return {"status": "used", "used": code.used}

        code.used = user_id
        code.usedtime = datetime.now()
        user.us = int(user.us or 0) + int(code.us or 0)
        session.commit()
        return {"status": "ok", "new_us": user.us, "code": register_code}


def old_create_gate(user_id: int, delay: float = 0.3) -> dict:
    data = sql_get_emby(tg=user_id)
    if not data:
        return {"status": "no_user"}
    time.sleep(delay)
    if data.embyid:
        return {"status": "has_account"}
    if int(data.us or 0) <= 0:
        return {"status": "no_qualification"}
    return {"status": "can_create", "us": data.us}


def fixed_create_gate(user_id: int, delay: float = 0.3) -> dict:
    time.sleep(delay)
    with Session() as session:
        user = session.query(Emby).filter(Emby.tg == user_id).with_for_update().first()
        if not user:
            return {"status": "no_user"}
        if user.embyid:
            return {"status": "has_account"}
        if int(user.us or 0) <= 0:
            return {"status": "no_qualification"}
        return {"status": "can_create", "us": user.us}


def run_same_user_multi_code(mode: str, delay: float) -> int:
    worker = old_redeem_register_code if mode == "old" else fixed_redeem_register_code
    print(f"[scenario] same-user-multi-code mode={mode}")
    with seeded_context(code_count=2, us=30) as ctx:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(worker, ctx.user_id, ctx.codes[0], delay),
                executor.submit(worker, ctx.user_id, ctx.codes[1], delay),
            ]
            results = [f.result() for f in as_completed(futures)]

        user = fetch_user_state(ctx.user_id)
        codes = [fetch_code_state(code) for code in ctx.codes]

    print("results:")
    for item in results:
        print(f"  {item}")
    print(f"user={user}")
    for item in codes:
        print(f"code={item}")

    success_count = sum(1 for item in results if item["status"] == "ok")
    consumed_count = sum(1 for item in codes if item["used"] is not None)
    if mode == "old":
        violated = success_count > 1 or consumed_count > 1
        print(f"reproduced={violated}")
        return 0 if violated else 1

    fixed_ok = success_count == 1 and consumed_count == 1 and int(user["us"] or 0) == 30
    print(f"fixed_ok={fixed_ok}")
    return 0 if fixed_ok else 1


def run_redeem_then_create(mode: str, delay: float) -> int:
    redeem_worker = old_redeem_register_code if mode == "old" else fixed_redeem_register_code
    create_worker = old_create_gate if mode == "old" else fixed_create_gate

    print(f"[scenario] redeem-then-create mode={mode}")
    with seeded_context(code_count=1, us=30) as ctx:
        create_result = {}
        redeem_result = {}

        def run_create():
            nonlocal create_result
            create_result = create_worker(ctx.user_id, delay)

        def run_redeem():
            nonlocal redeem_result
            time.sleep(delay / 2)
            redeem_result = redeem_worker(ctx.user_id, ctx.codes[0], 0)

        t1 = threading.Thread(target=run_create)
        t2 = threading.Thread(target=run_redeem)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        user = fetch_user_state(ctx.user_id)
        code = fetch_code_state(ctx.codes[0])

    print(f"create_result={create_result}")
    print(f"redeem_result={redeem_result}")
    print(f"user={user}")
    print(f"code={code}")

    if mode == "old":
        reproduced = create_result.get("status") == "no_qualification" and code["used"] is not None and int(user["us"] or 0) > 0
        print(f"reproduced={reproduced}")
        return 0 if reproduced else 1

    fixed_ok = create_result.get("status") == "can_create" and code["used"] is not None and int(user["us"] or 0) > 0
    print(f"fixed_ok={fixed_ok}")
    return 0 if fixed_ok else 1


def preview_targets() -> int:
    """--dry-run（默认）：只查询并打印"将会被删除的行"，不做任何写入（D-H5）。"""
    user_id = TEST_ID_BASE
    issuer_id = TEST_ID_BASE - 1
    codes = [f"race-{user_id}-{i}" for i in range(2)]

    with Session() as session:
        emby_rows = session.query(Emby).filter(Emby.tg.in_([user_id, issuer_id])).all()
        code_rows = session.query(Code).filter(Code.code.in_(codes)).all()

    print("[dry-run] 未执行任何写入/删除（加 --i-know-what-i-am-doing 才会真正运行）。")
    print(f"[dry-run] 将会删除的 emby 行（tg in {[user_id, issuer_id]}）：")
    for line in describe_rows(emby_rows, "tg", "emby"):
        print(line)
    print(f"[dry-run] 将会删除的 Rcode 行（code in {codes}）：")
    for line in describe_rows(code_rows, "code", "Rcode"):
        print(line)
    if not emby_rows and not code_rows:
        print("  （无匹配行）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproduce historical register-code concurrency races.")
    parser.add_argument(
        "scenario",
        choices=["same-user-multi-code", "redeem-then-create"],
        help="Which race to run.",
    )
    parser.add_argument(
        "--mode",
        choices=["old", "fixed"],
        default="old",
        help="Run the historical buggy flow or the current atomic flow.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Artificial delay in seconds used to widen the race window.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="显式指定数据库连接串；默认读取仓库根目录的 config.json。"
             "库名必须包含 'test'，否则拒绝执行（D-H5）。",
    )
    parser.add_argument(
        "--i-know-what-i-am-doing",
        action="store_true",
        help="确认会对目标测试库执行 DELETE / INSERT。不加该开关时等价于 --dry-run。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将会被删除的行，不执行任何写操作。",
    )
    args = parser.parse_args()

    global engine, Session
    database_url = load_database_url(args.database_url)
    db_name = parse_db_name(database_url)
    # 生产库保护：库名不符测试约定时直接拒绝（D-H5）
    assert_test_database_name(db_name)
    print(f"[guard] 目标库: {db_name}（已通过测试库命名检查）")

    engine = create_engine(
        database_url,
        echo=False,
        echo_pool=False,
        pool_size=8,
        connect_args={"init_command": "SET NAMES utf8mb4"},
    )
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    # 表名检查：确认连的是本项目的库结构（D-H5）
    assert_expected_tables(engine)

    if args.dry_run or not args.i_know_what_i_am_doing:
        if not args.dry_run:
            print("[guard] 未提供 --i-know-what-i-am-doing，自动降级为 --dry-run。")
        return preview_targets()

    if args.scenario == "same-user-multi-code":
        return run_same_user_multi_code(args.mode, args.delay)
    return run_redeem_then_create(args.mode, args.delay)


if __name__ == "__main__":
    raise SystemExit(main())
