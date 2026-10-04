#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
H1 回归测试：并发播放检测任务的「警告计数」丢失更新竞态。

背景（对抗性复核 H1）
----------------------
警告计数有两个写入方：
  A. 本检测任务 check_concurrent_play_limit()：读计数 → terminate_all_user_sessions()
     （**秒级 Emby I/O**）→ 绝对值写回 current_warns + 1
  B. 管理员在 /kk 面板点「🔄 重置警告」/「➖ 警告-1」/「➕ 警告+1」

修复前最坏序列：
  任务读到 5 → 任务进入秒级 I/O → 管理员重置为 0（面板提示"已重置为 0"）
  → 任务回来写 6 → **重置被静默抹掉**；且 6 ≥ 阈值时同一流程立刻 ban_user
  —— 管理员刚重置完，用户反而被自动封禁。

修复方式：把读计数挪到**写之前**，且读与写之间没有 await
（asyncio 协作式调度，因此这一段对事件循环原子）。

本测试用 mock 让 I/O 在飞行中真的去改一次数据库，从而**行为级**复现该序列，
而不是只做源码字符串断言 —— 否则把修复改回去测试照样全绿（复核已证明
这种"零判别力"是 M1/M2 逃逸的原因）。
"""

import ast
import asyncio
import sys
import textwrap
import types
from pathlib import Path

PASS, FAIL = 0, 0
SRC = Path(__file__).resolve().parent.parent / "bot/modules/extra/concurrent_play_monitor.py"
SRC_TEXT = SRC.read_text(encoding="utf-8")


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def extract(path, name):
    """从源码里抽出指定函数的完整定义（保持缩进原样，便于 exec）。"""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(text, node))
    raise AssertionError(f"源码里找不到函数 {name}")


def module_const(name):
    """取模块级常量（如 _WARN_N_PLACEHOLDER）的字面量值。"""
    for node in ast.parse(SRC_TEXT).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"源码里找不到常量 {name}")


# ───────────────────────── mock 环境 ─────────────────────────

class _Col:
    """模拟 Emby.tg，使 `Emby.tg == tg_id` 产出可识别的 where 条件。"""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return (self.name, other)


class _Emby:
    tg = _Col("tg")


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def make_ns(db, on_terminate=None, threshold=3, limit=2):
    """构造 check_concurrent_play_limit 的运行环境。

    db: {tg_id: Row}，Row.concurrent_warn_count 就是"数据库里的警告数"。
    on_terminate: 模拟"秒级 I/O 期间管理员动了计数"的回调，签名 (db) -> None。
    """
    ns = {}

    ns["config"] = types.SimpleNamespace(
        concurrent_play_limit_enabled=True,
        concurrent_play_limit=limit,
        concurrent_play_limit_whitelist=4,
        concurrent_play_limit_whitelist_enabled=True,
        concurrent_play_warn_threshold=threshold,
    )
    ns["Emby"] = _Emby
    ns["_WARN_N_PLACEHOLDER"] = module_const("_WARN_N_PLACEHOLDER")
    ns["_STOP_VERIFY_FOLLOWUP"] = 30

    logs = []
    ns["LOGGER"] = types.SimpleNamespace(
        error=lambda m, *a, **k: logs.append(("error", m)),
        warning=lambda m, *a, **k: logs.append(("warning", m)),
        info=lambda m, *a, **k: logs.append(("info", m)),
        debug=lambda m, *a, **k: logs.append(("debug", m)),
    )

    async def get_sessions_by_user():
        # 3 个流 > 上限 2，必然违规
        return {"emby-abc": [{"UserName": "xiaoya499", "NowPlayingItem": {"Name": f"M{i}"},
                              "Client": "TV"} for i in range(3)]}

    ns["get_sessions_by_user"] = get_sessions_by_user

    def sql_get_emby(tg=None, **kw):
        # 真实代码先用 emby id 当 tg 查一次（必然查不到），再走 sql_get_by_embyid
        return db.get(tg)

    def sql_get_by_embyid(embyid):
        for row in db.values():
            if row.embyid == embyid:
                return row
        return None

    def sql_update_emby(where, **kw):
        col, val = where
        assert col == "tg", where
        if val in db:
            db[val].__dict__.update(kw)

    ns["sql_get_emby"] = sql_get_emby
    ns["sql_get_by_embyid"] = sql_get_by_embyid
    ns["sql_update_emby"] = sql_update_emby

    ns["judge_admins"] = lambda tg: False

    async def is_emby_admin(embyid):
        return False

    ns["emby_mod"] = types.SimpleNamespace(is_emby_admin=is_emby_admin)

    calls = {"terminate": 0, "ban": [], "announce": [], "warn": []}

    async def terminate_all_user_sessions(emby_user_id, sessions, reason=""):
        calls["terminate"] += 1
        if on_terminate is not None:
            # ★ 关键：在这里（即"秒级 I/O 进行中"）模拟管理员改计数
            on_terminate(db)
        return 1, 0, ["s1"]

    ns["terminate_all_user_sessions"] = terminate_all_user_sessions

    async def _sync_kk_panels(uid=None):
        return None

    ns["_sync_kk_panels"] = _sync_kk_panels

    async def ban_user(emby_id, tg_id=None):
        calls["ban"].append((emby_id, tg_id))
        return True

    ns["ban_user"] = ban_user

    async def warn_user(tg_id, text):
        calls["warn"].append((tg_id, text))

    ns["warn_user"] = warn_user

    async def send_group_announcement(text):
        calls["announce"].append(text)

    ns["send_group_announcement"] = send_group_announcement

    ns["_schedule_stop_verify"] = lambda *a, **k: None
    ns["_now_str"] = lambda: "2026-10-04 22:00:00"

    return ns, calls, logs


FN_SRC = extract(SRC, "check_concurrent_play_limit")


def build_fn(ns):
    code = compile(FN_SRC, str(SRC), "exec")
    exec(code, ns)  # noqa: S102 - 被测代码就是本仓库源码
    return ns["check_concurrent_play_limit"]


def run_case(on_terminate, db, threshold=3):
    ns, calls, logs = make_ns(db, on_terminate=on_terminate, threshold=threshold)
    fn = build_fn(ns)
    asyncio.run(fn())
    return db, calls, logs


TG = 8638572039


def fresh_db(warns):
    return {TG: Row(tg=TG, embyid="emby-abc", name="xiaoya499", lv="b",
                    concurrent_warn_count=warns)}


print("════════ 1. 核心场景：I/O 期间管理员「🔄 重置警告」→ 重置不得被抹掉 ════════")
# 任务读到 5，进入秒级 I/O；期间管理员重置为 0；任务回来必须写 1（本次新违规），
# 而不是把重置抹掉写成 6。
db = fresh_db(5)


def admin_resets(_db):
    _db[TG].concurrent_warn_count = 0


db, calls, logs = run_case(admin_resets, db)
print(f"  最终 concurrent_warn_count = {db[TG].concurrent_warn_count}   （期望 1，修复前为 6）")
print(f"  ban_user 调用 = {calls['ban']}   （期望 []，修复前会误封）")
check("管理员的重置没有被静默抹掉（最终值 = 重置后的 0 + 本次 1 次违规 = 1）",
      db[TG].concurrent_warn_count == 1, f"实际 {db[TG].concurrent_warn_count}")
check("重置后未达阈值，不得误封用户", calls["ban"] == [], str(calls["ban"]))
check("确实执行了终止流", calls["terminate"] == 1, str(calls["terminate"]))
check("仍然发出了群通报", len(calls["announce"]) == 1, str(calls["announce"]))

msg = calls["announce"][0] if calls["announce"] else ""
print(f"  通报里的「累计警告」行: "
      f"{[l for l in msg.splitlines() if '累计警告' in l]}")
check("通报里的累计警告数字与数据库一致（都是 1）",
      "累计警告: **1**" in msg, msg[:400])
check("通报里不再出现两个互相矛盾的警告次数（修复前会是 6 与 1 并存）",
      "**6**" not in msg, msg[:400])
check("通报里没有残留占位符", "\x00" not in msg, repr(msg[:200]))

print()
print("════════ 2. 同类场景：I/O 期间管理员「➖ 警告-1」（5 → 4）════════")
db = fresh_db(5)


def admin_minus(_db):
    _db[TG].concurrent_warn_count = 4


db, calls, logs = run_case(admin_minus, db)
print(f"  最终 concurrent_warn_count = {db[TG].concurrent_warn_count}   （期望 5，修复前为 6）")
check("管理员 -1 的结果被保留（4 + 1 = 5）", db[TG].concurrent_warn_count == 5,
      f"实际 {db[TG].concurrent_warn_count}")
check("5 ≥ 阈值 3 → 应当封禁（这是修复后的正确行为）", len(calls["ban"]) == 1, str(calls["ban"]))

print()
print("════════ 3. 无并发干扰：正常自增与阈值封禁仍工作 ════════")
db = fresh_db(0)
db, calls, logs = run_case(None, db)
check("无干扰时 0 → 1", db[TG].concurrent_warn_count == 1, str(db[TG].concurrent_warn_count))
check("1 < 阈值 3 → 不封禁", calls["ban"] == [], str(calls["ban"]))
check("文案提示还需再犯 2 次", "再犯 **2** 次" in (calls["announce"][0] if calls["announce"] else ""),
      (calls["announce"][0] if calls["announce"] else "")[:400])

db = fresh_db(2)
db, calls, logs = run_case(None, db)
check("无干扰时 2 → 3", db[TG].concurrent_warn_count == 3, str(db[TG].concurrent_warn_count))
check("3 ≥ 阈值 3 → 封禁", len(calls["ban"]) == 1, str(calls["ban"]))

print()
print("════════ 4. 源码不变量：写计数前的「重新读」必须紧邻写、且中间无 await ════════")
fn_node = next(n for n in ast.parse(SRC_TEXT).body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "check_concurrent_play_limit")
# 写计数在 `for emby_user_id, sessions in ...` 循环体内，所以要在循环体这一层分析，
# 而不是函数顶层。
_loop = next(n for n in fn_node.body if isinstance(n, ast.For))
body = _loop.body


def is_await(n):
    # 既覆盖 `await f()`（ast.Expr），也覆盖 `x = await f()`（ast.Assign）——
    # 终止流那句正是后者，漏掉它就会把最大的竞态窗口判成"没有 await"。
    return any(isinstance(x, ast.Await) for x in ast.walk(n))


def is_sql_update(n):
    return (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name) and n.value.func.id == "sql_update_emby"
            and any(k.arg == "concurrent_warn_count" for k in n.value.keywords))


def is_sql_get(n):
    return (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name) and n.value.func.id == "sql_get_emby")


write_idx = next(i for i, n in enumerate(body) if is_sql_update(n))
print(f"  sql_update_emby(concurrent_warn_count=...) 位于函数体第 {write_idx} 条语句")

read_idx = max(i for i, n in enumerate(body) if i < write_idx and is_sql_get(n))
print(f"  其前最近一次 sql_get_emby 位于第 {read_idx} 条语句")
between = body[read_idx + 1:write_idx]
print(f"  两者之间的语句数 = {len(between)}，其中 await 数 = {sum(1 for n in between if is_await(n))}")
check("写计数之前有一次新的 sql_get_emby（不是复用循环开头读到的值）",
      read_idx < write_idx, f"read={read_idx} write={write_idx}")
check("读与写之间没有 await（否则又是竞态窗口）",
      not any(is_await(n) for n in between),
      str([ast.unparse(n)[:60] for n in between]))
check("读与写之间没有调用 terminate_all_user_sessions（I/O 必须在其之前）",
      not any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
              and getattr(n.value.func, "id", "") == "terminate_all_user_sessions"
              for n in between),
      str([ast.unparse(n)[:60] for n in between]))

# 循环开头那次读必须已删除，否则容易被人"顺手改回去"
early_reads = [n for n in ast.walk(fn_node)
               if isinstance(n, ast.Assign) and isinstance(n.value, ast.BoolOp)
               and any(isinstance(v, ast.Attribute) and v.attr == "concurrent_warn_count"
                       for v in ast.walk(n.value))]
check("循环开头不再读 concurrent_warn_count（避免被误用为写入依据）",
      early_reads == [], str([f"第{n.lineno}行 {ast.unparse(n)}" for n in early_reads]))

print()
print("════════════════════════════════════════")
print(f"  结果：PASS={PASS}  FAIL={FAIL}")
print("════════════════════════════════════════")
sys.exit(1 if FAIL else 0)
