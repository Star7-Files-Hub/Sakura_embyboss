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
import html
import re
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
        # 真实实现返回 bool；kick_user_streams 会检查这个返回值（False = 落库失败
        # 就绝不禁用），所以替身必须如实返回 True，否则会把"落库失败"路径当成正常路径。
        calls["db_writes"].append(dict(kw))
        if val in db:
            db[val].__dict__.update(kw)
        return True

    ns["sql_get_emby"] = sql_get_emby
    ns["sql_get_by_embyid"] = sql_get_by_embyid
    ns["sql_update_emby"] = sql_update_emby

    ns["judge_admins"] = lambda tg: False

    async def is_emby_admin(embyid):
        return False

    ns["emby_mod"] = types.SimpleNamespace(is_emby_admin=is_emby_admin)

    calls = {"terminate": 0, "ban": [], "announce": [], "warn": [], "db_writes": [],
             "restore_sweep": [], "is_disabled": [], "set_disabled": [], "kick_worker": [],
             "kick_verify": []}

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
    # 2026-10-06 二轮：kick 路径不再用「按旧 session id」的复验，改成
    # `_schedule_kick_verify(emby_user_id, user_name, wait, stream_count)`
    # （按 UserId 复查，因为解封后重连会生成新 session id）。
    # 本套件只关心警告计数竞态，这里换成记录替身即可；复验的判定语义由
    # tests/test_kick_verify_userid.py 与 tests/test_terminate_verify.py 覆盖。
    def _schedule_kick_verify(emby_user_id, user_name, wait, stream_count):
        calls["kick_verify"].append((emby_user_id, user_name, wait, stream_count))

    ns["_schedule_kick_verify"] = _schedule_kick_verify
    ns["_now_str"] = lambda: "2026-10-04 22:00:00"
    # 真实 escape_markdown（_mention 依赖；只用到 re / html）
    ns["re"] = re
    ns["html"] = html
    exec(compile(extract(MSG_UTILS, "escape_markdown"), str(MSG_UTILS), "exec"), ns)

    # ── 2026-10-06 新增：check_concurrent_play_limit 现在会真的去「临时封禁踢流」 ──
    #
    # 停流路径改了：先下发 Stop（对本服务器客户端无效），未达阈值时改成
    # `kick_user_streams()`（临时禁用账号几十秒后自动还原）。所以本套件必须
    # 把 **真实的** kick_user_streams 一起抽出来执行 —— 否则它只会在运行时报
    # NameError（这正是本套件上一次崩溃的原因），而不是真正验证竞态。
    #
    # 只把两样东西换成替身，理由明确：
    #   · restore_pending_kicks —— 它内部 `from bot.sql_helper.sql_emby import ...`
    #     会去连真实数据库；本套件只关心警告计数竞态，替身直接返回 0 条。
    #   · _kick_restore_worker  —— 真实实现会 sleep 45 秒；它的「到期还原 /
    #     被取消也要还原」由 tests/test_temp_kick.py 行为级覆盖，这里换成 no-op，
    #     避免每次跑本套件都挂一个 45 秒的待取消任务。
    from datetime import datetime, timedelta, timezone

    ns["asyncio"] = asyncio
    ns["datetime"] = datetime
    ns["timedelta"] = timedelta
    ns["timezone"] = timezone
    ns["_KICK_HOLD_SECONDS"] = module_const("_KICK_HOLD_SECONDS")
    ns["_KICK_VERIFY_ATTEMPTS"] = module_const("_KICK_VERIFY_ATTEMPTS")
    ns["_KICK_VERIFY_INTERVAL"] = 0
    ns["_KICK_TASKS"] = set()

    async def restore_pending_kicks(only_expired=False):
        calls["restore_sweep"].append(only_expired)
        return 0

    ns["restore_pending_kicks"] = restore_pending_kicks

    async def _kick_restore_worker(emby_user_id, tg_id, hold_seconds):
        calls["kick_worker"].append((emby_user_id, tg_id, hold_seconds))

    ns["_kick_restore_worker"] = _kick_restore_worker

    class _EmbyDouble:
        """真实 kick_user_streams 依赖的两个只读/写入方法。"""

        async def is_user_disabled(self, emby_id):
            calls["is_disabled"].append(emby_id)
            return False          # 用户本来是启用的 → 允许临时封禁

        async def set_user_disabled(self, emby_id, disabled):
            calls["set_disabled"].append((emby_id, disabled))
            return True

    ns["emby"] = _EmbyDouble()

    return ns, calls, logs


# 被测函数集合：被测函数本身 + 它真正调用到的新函数。
# 新增停流路径后，check_concurrent_play_limit 会调用 kick_user_streams()，
# 而 kick_user_streams 依赖 _utcnow()。把它们一起抽出来执行，才能继续做
# 行为级验证（而不是只做源码字符串断言）。
EXTRACTED_FUNCS = ("check_concurrent_play_limit", "kick_user_streams", "_utcnow",
                   "_mention")

# `_mention` 依赖真 `escape_markdown`。**不能**桩成 lambda s: s —— 那样"名字里的
# Markdown 特殊字符被转义"就没有证据了。抽 msg_utils.py 里的真实纯函数。
MSG_UTILS = SRC.parent.parent.parent / "func_helper/msg_utils.py"
FN_SRC = "\n\n\n".join(extract(SRC, n) for n in EXTRACTED_FUNCS)


def referenced_globals(src):
    """列出这段源码里**引用了但未在本地绑定**的名字（即必须由命名空间提供的）。

    只减掉真正在本地绑定的名字（赋值目标、for 目标、函数参数、with ... as、
    except ... as、推导式目标、import 别名），否则局部变量会被误报成缺失的替身。
    """
    tree = ast.parse(src)
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.Lambda):
            bound.update(a.arg for a in node.args.args)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - bound


def env_selfcheck(ns):
    """命名空间自检：抽取的函数引用的每个全局都必须有替身。

    这正是在修本套件上一次的崩溃：新增了 kick_user_streams 调用、但抽取/替身
    没跟上，结果跑到一半抛 NameError，把「测试环境没搭好」伪装成「测试崩溃」。
    现在改成提前一次性列出缺哪些名字，报 FAIL 而不是崩；并且**返回缺失列表**，
    调用方据此跳过本轮执行 —— 否则 NameError 会崩在汇总之前，把一条清晰的
    "替身缺全局"变成一堆看不出原因的 traceback。
    """
    import builtins
    missing = sorted(n for n in referenced_globals(FN_SRC)
                     if n not in ns and not hasattr(builtins, n))
    check("环境自检：被测函数引用的全局在替身命名空间里都有定义", missing == [],
          f"缺少 {missing}")
    return missing


def build_fn(ns):
    code = compile(FN_SRC, str(SRC), "exec")
    exec(code, ns)  # noqa: S102 - 被测代码就是本仓库源码
    return ns["check_concurrent_play_limit"]


def run_case(on_terminate, db, threshold=3):
    ns, calls, logs = make_ns(db, on_terminate=on_terminate, threshold=threshold)
    fn = build_fn(ns)          # 先把抽取的函数装进命名空间
    missing = env_selfcheck(ns)   # 再自检：还缺谁就一次说清，不要跑到一半 NameError
    if missing:
        print(f"  ⚠️ 替身缺全局 {missing}，跳过本轮执行（不崩，让汇总照常打印）")
        return db, calls, logs
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
# 未达阈值时，新的停流路径（临时封禁踢流）必须真的被执行到 —— 否则本套件只是
# "碰巧没崩"，并没有覆盖真实路径；同时证明重置在这次 kick 落库之后依然是 1。
check("未达阈值 → 真的走了临时封禁踢流（kick_until 已落库 + 已写 IsDisabled=True）",
      calls["set_disabled"] == [("emby-abc", True)]
      and any("kick_until" in w for w in calls["db_writes"]),
      f"set_disabled={calls['set_disabled']} writes={calls['db_writes']}")
# kick 路径的复验必须按 **UserId** 调度（不是按旧 session id）：解封后用户重连会
# 生成新的 session id，按旧 id 查永远查不到 → 无论有没有重连都会报"已断开"（假证据）。
check("未达阈值 → 复验按 UserId 调度，且 wait 晚于解封时刻（> hold_seconds）",
      len(calls["kick_verify"]) == 1
      and calls["kick_verify"][0][0] == "emby-abc"
      and calls["kick_verify"][0][2] > 45
      and calls["kick_verify"][0][3] == 3,
      f"kick_verify={calls['kick_verify']}")

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
