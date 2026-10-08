#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_temp_kick.py —— 「临时封禁踢流」行为级测试套件。

背景
----
用户报障「终止流不成功，直到被封禁才终止成功」。实测结论：
`POST /Sessions/{Id}/Playing/Stop` 在这台 Emby 4.10 上对现有客户端 100% 无效
（HTTP 204 但 30 秒后仍在播），唯一真正能停流的是把用户策略 `IsDisabled` 置真。
于是实现了「临时封禁踢流」：禁用 → 落库到期时间 → 后台自动还原 + 崩溃自恢复。

本套件只做**行为级**验证
------------------------
用 AST 从真实源码里抽出函数、exec 进替身命名空间，让替身记录**调用序列与参数**，
再断言这些序列。**刻意不做**「源码里出现某字符串」这类字节相邻断言 —— 那种断言
在本项目已经翻过一次车（改坏实现测试照样全绿）。每条断言都必须能被变异测试证伪
（见 /tmp/t39/tests-report.md 的变异实测）。

覆盖 16 条行为：
  1  正常踢流            2  顺序不可颠倒（先落库后禁用）
  3  落库失败则不禁用     4  禁用失败则清标记
  5  护栏：已被禁用不得踢  6  护栏：状态读不到不得踢
  7  缺 tg_id 不得踢      8  还原成功清标记
  9  还原失败保留标记     10 接管护栏（kick_until 已清 → 绝不动手）
  11 ban_user 清标记      12 启动自恢复（未到期也还原）
  13 周期巡检（未到期跳过） 14 worker 被取消也要还原
  15 复验时机（wait > hold_seconds） 16 ban 与 kick 互斥

运行：python3 tests/test_temp_kick.py      （不依赖 pytest，与其它套件一致）
"""

import ast
import asyncio
import builtins
import html
import re
import sys
import textwrap
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

PASS, FAIL = 0, 0

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "bot/modules/extra/concurrent_play_monitor.py"
SRC_TEXT = SRC.read_text(encoding="utf-8")

TG = 1156115326
EMBY = "f58ac4d2b82341b492e7d5309a024b39"


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


# ─────────────────────── AST 抽取（真实源码） ───────────────────────

def extract(name):
    """从真实源码里抽出指定函数的完整定义（保持缩进原样，便于 exec）。"""
    for node in ast.parse(SRC_TEXT).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(SRC_TEXT, node))
    raise AssertionError(f"源码里找不到函数 {name}")


def module_const(name):
    for node in ast.parse(SRC_TEXT).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"源码里找不到常量 {name}")


EXTRACTED = ("_utcnow", "kick_user_streams", "_kick_restore_worker", "_restore_kick",
             "restore_pending_kicks", "ban_user", "check_concurrent_play_limit",
             "_schedule_kick_verify", "_should_alert_restore_failure",
             "_restore_alert_key", "_clear_restore_alert", "_mention")

# _mention 依赖真 `escape_markdown`。**不能**桩成 lambda s: s —— 那样"名字里的
# Markdown 特殊字符被转义"这条断言就失去意义了。这里把 msg_utils.py 里的**真实**
# 纯函数抽出来（只依赖 re / html）。
MSG_SRC_TEXT = (REPO / "bot/func_helper/msg_utils.py").read_text(encoding="utf-8")


def extract_from(src_text, name):
    hits = [n for n in ast.walk(ast.parse(src_text))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(hits) != 1:
        raise AssertionError(f"{name} 命中 {len(hits)} 次（期望 1 次）")
    return textwrap.dedent(ast.get_source_segment(src_text, hits[0]))


ESCAPE_MD_SRC = extract_from(MSG_SRC_TEXT, "escape_markdown")
FN_SRC = "\n\n\n".join(extract(n) for n in EXTRACTED)

HOLD = module_const("_KICK_HOLD_SECONDS")
ATTEMPTS = module_const("_KICK_VERIFY_ATTEMPTS")
ALERT_INTERVAL = module_const("_RESTORE_ALERT_INTERVAL")   # 还原失败通报的节流窗口（秒）


def referenced_globals(src):
    """引用了但未在本地绑定的名字（= 必须由替身命名空间提供的名字）。"""
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
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 嵌套 def/class 的名字在 AST 里是**字符串属性**，不是 Name(Store) 节点。
            # 不补进来的话，被测函数内部定义的闭包（例如 kick_user_streams 里的
            # _persist_kick_marker）会被误判成"引用了不存在的全局替身"。
            bound.add(node.name)
        elif isinstance(node, ast.Lambda):
            bound.update(a.arg for a in node.args.args)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - bound


# ───────── bot.sql_helper.sql_emby 替身（restore_pending_kicks 内部 import 它） ─────────
#
# restore_pending_kicks 里写的是 `from bot.sql_helper.sql_emby import sql_get_pending_kicks`
# （函数内局部 import），必须让它 import 到替身，而不是去连真实数据库。
PENDING = []          # [(tg, embyid, kick_until)]


def _install_stub_modules():
    bot = types.ModuleType("bot")
    bot.__path__ = []
    sql_helper = types.ModuleType("bot.sql_helper")
    sql_helper.__path__ = []
    sql_emby = types.ModuleType("bot.sql_helper.sql_emby")
    sql_emby.sql_get_pending_kicks = lambda: list(PENDING)
    bot.sql_helper = sql_helper
    sql_helper.sql_emby = sql_emby
    sys.modules["bot"] = bot
    sys.modules["bot.sql_helper"] = sql_helper
    sys.modules["bot.sql_helper.sql_emby"] = sql_emby


_install_stub_modules()


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class EmbyCol:
    """让 `Emby.tg == tg_id` 产出可识别的 where 条件 ("tg", tg_id)。"""

    def __eq__(self, other):
        return ("tg", other)


def make_env(*, is_disabled=False, persist_ok=True, disable_ok=True,
             restore_write_ok=True, ban_ok=True, row_present=True,
             kick_until_set=False, kick_until_value=None,
             warn_count=0, threshold=3, streams=3, limit=2, session_ids=True,
             attempts=ATTEMPTS, interval=0, pending=None, rows=None):
    """构造被测函数的运行环境，返回 (ns, ev)。

    ev 记录所有替身收到的调用，供行为断言使用：
      events       [("persist", 字段) | ("disable", True|False)] 副作用先后顺序
      db           sql_update_emby 每次收到的 kwargs
      set_disabled [(emby_id, disabled)]   —— 写用户策略（踢流/还原）
      is_disabled  [emby_id]               —— 读用户策略
      policy       [(emby_id, disable)]    —— ban_user 走的 emby_change_policy
      announce     [text] / warn [(tg, text)] / sched [(ids, name, wait)]
      kick_worker  [(emby_id, tg, hold)]   —— 还原任务被调度（默认用替身，见下）
      kick_sched   [(emby_id, name, wait, stream_count)] —— 按 UserId 的复验被调度
    """
    ev = {"events": [], "db": [], "set_disabled": [], "is_disabled": [], "policy": [],
          "announce": [], "warn": [], "sched": [], "restore_kick": [], "logs": [],
          "kick_worker": [], "kick_sched": []}

    if pending is None:
        PENDING.clear()
    else:
        PENDING[:] = list(pending)

    ns = {}
    ns["asyncio"] = asyncio
    ns["datetime"] = datetime
    ns["timedelta"] = timedelta
    ns["timezone"] = timezone
    ns["_KICK_HOLD_SECONDS"] = HOLD
    ns["_KICK_VERIFY_ATTEMPTS"] = attempts
    ns["_KICK_VERIFY_INTERVAL"] = interval     # 0 = 不在测试里真睡 3 秒
    ns["_KICK_TASKS"] = set()
    ns["_RESTORE_ALERT_INTERVAL"] = ALERT_INTERVAL
    ns["_RESTORE_ALERTS"] = {}      # 每个环境一份，避免用例之间互相节流
    ns["_STOP_VERIFY_FOLLOWUP"] = module_const("_STOP_VERIFY_FOLLOWUP")
    ns["_WARN_N_PLACEHOLDER"] = module_const("_WARN_N_PLACEHOLDER")
    # 真实的 escape_markdown（只依赖 re / html）
    ns["re"] = re
    ns["html"] = html
    exec(compile(ESCAPE_MD_SRC, "msg_utils.py", "exec"), ns)   # noqa: S102

    def _mk_logger(level):
        def _log(msg, *a, **k):
            ev["logs"].append((level, str(msg)))
        return _log

    ns["LOGGER"] = types.SimpleNamespace(
        info=_mk_logger("info"), warning=_mk_logger("warning"),
        error=_mk_logger("error"), debug=_mk_logger("debug"))

    ns["Emby"] = type("Emby", (), {"tg": EmbyCol()})

    # ── 数据库替身（按 where 里的 tg 精确落到那一行） ──
    if rows is None:
        row = Row(tg=TG, embyid=EMBY, name="toe", lv="b",
                  concurrent_warn_count=warn_count)
        if kick_until_set:
            row.kick_until = kick_until_value if kick_until_value is not None else \
                datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=HOLD)
        rows = {TG: row} if row_present else {}
    ev["rows"] = rows

    def sql_get_emby(tg=None, **kw):
        # 真实 `sql_get_emby` 在**查询抛异常时也返回 None**（sql_emby.py 的
        # `except → return None`）。桩必须照这个行为来，否则「读失败被当成行不存在」
        # 这条 P2 回归根本不可能被测出来 —— 变异时会看到桩照样返回那一行，
        # 于是错的东西反而"通过"。
        if ev.get("db_read_raises"):
            return None
        return rows.get(tg)

    # 真实 `_restore_kick` 用的是三态版本（区分「查不到」与「查失败」），
    # 默认按"读成功"返回，好让既有场景语义不变。
    def sql_get_emby_checked(tg=None, **kw):
        if ev.get("db_read_raises"):
            return None, False
        return rows.get(tg), True

    def sql_get_by_embyid(embyid):
        for r in rows.values():
            if r.embyid == embyid:
                return r
        return None

    def sql_update_emby(where, **kw):
        ev["db"].append(dict(kw))
        if "kick_until" in kw:
            ev["events"].append(("persist", "kick_until"))
        _, target = where            # ("tg", tg_id)
        target_row = rows.get(target)
        # 只有写入成功才真的落库：替身若"返回 False 却照样改库"，就会把
        # 「清标记失败但库里仍残留」这种真实状态掩盖掉（8i 正是要钉这一点）。
        if persist_ok and target_row is not None:
            target_row.__dict__.update(kw)
        return persist_ok

    ns["sql_get_emby"] = sql_get_emby
    ns["sql_get_emby_checked"] = sql_get_emby_checked
    ns["sql_get_by_embyid"] = sql_get_by_embyid
    ns["sql_update_emby"] = sql_update_emby

    # ── Emby 替身 ──
    # 中途可翻转：错误原因变化场景需要"先写入失败、再回读失败"，所以写入结果与
    # 回读结果都必须是可变状态，而不是闭包里的常量。
    emby_state = {"is_disabled": is_disabled, "restore_write_ok": restore_write_ok,
                  "disable_ok": disable_ok, "ban_ok": ban_ok}

    class EmbyDouble:
        state = emby_state

        async def is_user_disabled(self, emby_id):
            ev["is_disabled"].append(emby_id)
            return self.state["is_disabled"]

        async def set_user_disabled(self, emby_id, disabled, before_write=None):
            ev["set_disabled"].append((emby_id, disabled))
            # 真实实现把「落库」放在**锁内、POST 之前**（`before_write` 回调），
            # 所以调用顺序必须是 persist → disable。替身也照这个顺序记录，
            # 否则「顺序不可颠倒」那条断言会测到一个假的顺序。
            if before_write is not None:
                if not await before_write():
                    # 落库失败 → 真实实现直接放弃这次写入，一次 POST 都不发
                    ev["events"].append(("before_write_failed", disabled))
                    return False
            ev["events"].append(("disable", disabled))
            return self.state["disable_ok"] if disabled else self.state["restore_write_ok"]

        async def emby_change_policy(self, emby_id, admin=False, disable=False):
            ev["policy"].append((emby_id, disable))
            return self.state["ban_ok"]

    ns["emby"] = EmbyDouble()
    ev["emby_state"] = emby_state

    # ── 通知 / 复验替身 ──
    async def send_group_announcement(text):
        ev["announce"].append(text)

    async def warn_user(tg_id, text):
        ev["warn"].append((tg_id, text))

    def _schedule_stop_verify(session_ids, user_name, wait=None, emby_user_id=None,
                              announce_group=True):
        # task-45：回退路径也必须带上 emby_user_id（否则又退回"按旧 session id 复验"）
        ev["sched"].append((list(session_ids), user_name, wait, emby_user_id, announce_group))

    # 2026-10-06 二轮：kick 路径改用**按 UserId** 的复验 `_verify_kick_followup`，
    # 调度函数 `_schedule_kick_verify` 是真实源码（已抽取），这里只把「复验本体」
    # 换成记录替身 —— 这样既验证了真实调度函数把参数原样透传，又不会真等 60 秒。
    async def _verify_kick_followup(emby_user_id, user_name, wait, stream_count,
                                   announce_group=True):
        ev["kick_sched"].append((emby_user_id, user_name, wait, stream_count, announce_group))

    ns["send_group_announcement"] = send_group_announcement
    ns["warn_user"] = warn_user
    ns["_schedule_stop_verify"] = _schedule_stop_verify
    ns["_verify_kick_followup"] = _verify_kick_followup
    ns["_VERIFY_TASKS"] = set()

    # ── check_concurrent_play_limit 依赖 ──
    ns["config"] = types.SimpleNamespace(
        concurrent_play_limit_enabled=True, concurrent_play_limit=limit,
        concurrent_play_limit_whitelist=4, concurrent_play_limit_whitelist_enabled=True,
        concurrent_play_warn_threshold=threshold)
    ns["judge_admins"] = lambda tg: False

    async def is_emby_admin(embyid):
        return False

    ns["emby_mod"] = types.SimpleNamespace(is_emby_admin=is_emby_admin)
    ns["_now_str"] = lambda: "2026-10-06 12:00:00"

    async def get_sessions_by_user():
        out = []
        for i in range(streams):
            s = {"UserName": "toe", "Client": "CapyPlayer", "NowPlayingItem": {"Name": f"剧{i}"}}
            if session_ids:
                s["Id"] = f"s{i}"
            out.append(s)
        return {EMBY: out}

    ns["get_sessions_by_user"] = get_sessions_by_user

    async def terminate_all_user_sessions(emby_user_id, sessions, reason=""):
        ids = [s["Id"] for s in sessions if s.get("Id")]
        return len(ids), 0, ids

    ns["terminate_all_user_sessions"] = terminate_all_user_sessions

    async def _sync_kk_panels(uid=None):
        return None

    ns["_sync_kk_panels"] = _sync_kk_panels

    # ── 装入真实函数（抽取自源码） ──
    exec(compile(FN_SRC, str(SRC), "exec"), ns)  # noqa: S102 - 被测代码就是本仓库源码

    # 默认把「还原任务」换成替身：真实 worker 会 sleep hold_seconds(=HOLD)，在
    # asyncio.run 收尾时被取消 → 会顺带调用 _restore_kick → 污染 set_disabled/events
    # 记录，让"踢流本身"的断言变得不确定。第 14 条专门用真实 worker 验证取消语义。
    # 可伪造的时钟：节流测试要"推进 1800 秒"，不能真等。默认冻结在真实当前时刻，
    # 所以既有用例（kick_until 比较、巡检到期判断）行为完全不变。
    clock = {"now": datetime.now(timezone.utc).replace(tzinfo=None)}
    ns["_utcnow"] = lambda: clock["now"]
    ev["clock"] = clock

    ns["_real_kick_restore_worker"] = ns["_kick_restore_worker"]

    async def _kick_restore_worker_stub(emby_user_id, tg_id, hold_seconds):
        ev["kick_worker"].append((emby_user_id, tg_id, hold_seconds))

    ns["_kick_restore_worker"] = _kick_restore_worker_stub
    return ns, ev


def env_selfcheck(ns):
    missing = sorted(n for n in referenced_globals(FN_SRC)
                     if n not in ns and not hasattr(builtins, n))
    check("环境自检：被测函数引用的全局在替身命名空间里都有定义", missing == [],
          f"缺少 {missing}")


async def _kick_and_drain(ns, emby_id, tg_id):
    """调用 kick 并让被调度的还原任务（替身）真正跑完，使记录确定。"""
    res = await ns["kick_user_streams"](emby_id, tg_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return res


def kick(ns, emby_id=EMBY, tg_id=TG):
    return asyncio.run(_kick_and_drain(ns, emby_id, tg_id))


def restore(ns, emby_id=EMBY, tg_id=TG, source="到期"):
    return asyncio.run(ns["_restore_kick"](emby_id, tg_id, source))


print("=" * 78)
print("0. 环境自检（抽取的函数引用的每个全局都必须有替身，否则报 FAIL 而不是崩）")
print("=" * 78)
_ns0, _ev0 = make_env()
env_selfcheck(_ns0)
check("kick_user_streams / _restore_kick / ban_user 等 7 个函数都已从真实源码抽出",
      all(callable(_ns0.get(n)) for n in EXTRACTED), str(EXTRACTED))
check("常量与源码一致（hold>0, attempts>=2）", HOLD > 0 and ATTEMPTS >= 2,
      f"hold={HOLD} attempts={ATTEMPTS}")

print()
print("=" * 78)
print("1. 正常踢流：本来启用的用户 → 落库 kick_until → 写 IsDisabled=True")
print("=" * 78)
ns, ev = make_env(is_disabled=False, persist_ok=True, disable_ok=True)
before = ns["_utcnow"]()
res = kick(ns)
after = ns["_utcnow"]()
check("1a 返回 disabled=True", res["disabled"] is True, str(res))
check("1b error 为空（没有任何护栏触发）", res["error"] is None, str(res))
check("1c hold_seconds = 源码常量 %ds" % HOLD, res["hold_seconds"] == HOLD, str(res))
check("1d until 约为 now+hold（UTC naive，与落库口径一致）",
      res["until"] is not None
      and timedelta(seconds=HOLD - 5) <= res["until"] - before <= timedelta(seconds=HOLD + 5)
      and timedelta(seconds=HOLD - 5) <= res["until"] - after <= timedelta(seconds=HOLD + 5),
      f"until={res['until']} before={before} after={after}")
check("1e kick_until 已落库", any("kick_until" in w for w in ev["db"]), str(ev["db"]))
check("1f 落库的值就是返回的 until（不是另算一个时间）",
      any(w.get("kick_until") == res["until"] for w in ev["db"]), str(ev["db"]))
check("1g 写了 IsDisabled=True（恰好一次）",
      ev["set_disabled"] == [(EMBY, True)], str(ev["set_disabled"]))
check("1h 先读了当前禁用状态（护栏一必须先于任何写操作）",
      ev["is_disabled"] == [EMBY], str(ev["is_disabled"]))
check("1i 已调度还原任务，且保持时长 = hold_seconds",
      ev["kick_worker"] == [(EMBY, TG, HOLD)], str(ev["kick_worker"]))

print()
print("=" * 78)
print("2. 顺序不可颠倒：必须先落库 kick_until，再写 IsDisabled=True")
print("=" * 78)
check("2a 副作用序列恰好是 [落库 kick_until, 写 IsDisabled=True]",
      ev["events"] == [("persist", "kick_until"), ("disable", True)], str(ev["events"]))
check("2b 落库次数 == 1 且禁用次数 == 1（没有额外的写）",
      len([w for w in ev["db"] if "kick_until" in w]) == 1 and len(ev["set_disabled"]) == 1,
      f"db={ev['db']} set={ev['set_disabled']}")

print()
print("=" * 78)
print("3. 落库失败 → 绝不禁用（否则「禁用成功、落库失败」会永久锁死用户）")
print("=" * 78)
ns, ev = make_env(persist_ok=False)
res = kick(ns)
check("3a error = persist_failed", res["error"] == "persist_failed", str(res))
check("3b disabled=False", res["disabled"] is False, str(res))
# 【断言口径在 P1 修复后更新过】落库现在发生在 `set_user_disabled` **锁内**的
# `before_write` 回调里（这样「落库 + 写策略」才是一个原子临界区，见 kick_user_streams
# 的注释）。所以 `set_user_disabled` 现在**会被调用**——真正必须钉死的不变量是
# 「**一次策略写入都没发生**」，那才是"绝不禁用"的可观测含义。
check("3c 落库失败时一次策略写入都没发生（没有 disable 事件）",
      not [e for e in ev["events"] if e[0] == "disable"],
      str(ev["events"]))
check("3c2 set_user_disabled 虽然被调用，但回调失败后直接放弃（标记为 before_write_failed）",
      ("before_write_failed", True) in ev["events"], str(ev["events"]))
check("3d 没有创建还原任务（不会留下无主任务）", len(ns["_KICK_TASKS"]) == 0,
      str(ns["_KICK_TASKS"]))
check("3e 记录了 error 日志（便于运维发现落库故障）",
      any(lvl == "error" for lvl, _ in ev["logs"]), str(ev["logs"]))

print()
print("=" * 78)
print("4. 禁用失败 → 必须清掉 kick_until（不留无意义的待还原记录）")
print("=" * 78)
ns, ev = make_env(disable_ok=False)
res = kick(ns)
row = list(ev["rows"].values())[0]
check("4a error = disable_failed", res["error"] == "disable_failed", str(res))
check("4b 清标记：有一条把 kick_until 置 None 的写入",
      any(w.get("kick_until", "缺失") is None for w in ev["db"]), str(ev["db"]))
check("4c 数据库里 kick_until 最终为 None", getattr(row, "kick_until", None) is None,
      str(row.__dict__))
check("4d 没有创建还原任务", len(ns["_KICK_TASKS"]) == 0, str(ns["_KICK_TASKS"]))

print()
print("=" * 78)
print("5. 【护栏】用户已被禁用 → 不得踢（否则会把管理员的封禁当成自己的、HOLD 秒后解开）")
print("=" * 78)
ns, ev = make_env(is_disabled=True)
res = kick(ns)
check("5a error = already_disabled", res["error"] == "already_disabled", str(res))
check("5b disabled=False", res["disabled"] is False, str(res))
check("5c 一次都没有写库（不落 kick_until）", ev["db"] == [], str(ev["db"]))
check("5d 一次都没有调 set_user_disabled（不会去动管理员的封禁）",
      ev["set_disabled"] == [], str(ev["set_disabled"]))
check("5e 没有创建还原任务（否则 hold_seconds 后会解开管理员的封禁）",
      len(ns["_KICK_TASKS"]) == 0, str(ns["_KICK_TASKS"]))

print()
print("=" * 78)
print("6. 【护栏】读不到禁用状态（None）→ 不得踢（还原时不知道该还原成什么）")
print("=" * 78)
ns, ev = make_env(is_disabled=None)
res = kick(ns)
check("6a error = state_unknown", res["error"] == "state_unknown", str(res))
check("6b disabled=False", res["disabled"] is False, str(res))
check("6c 没有写库", ev["db"] == [], str(ev["db"]))
check("6d 没有调 set_user_disabled", ev["set_disabled"] == [], str(ev["set_disabled"]))
check("6e 区分「未禁用(False)」与「查不到(None)」：None 不得被当成 False",
      res["error"] == "state_unknown" and res["disabled"] is False, str(res))

print()
print("=" * 78)
print("7. 缺少 tg_id → 不得踢（落不了库就禁用 = 进程死掉后无人知道该给谁解封）")
print("=" * 78)
ns, ev = make_env()
res = kick(ns, tg_id=None)
check("7a error = missing_tg_id", res["error"] == "missing_tg_id", str(res))
check("7b 没有写库", ev["db"] == [], str(ev["db"]))
check("7c 没有调 set_user_disabled", ev["set_disabled"] == [], str(ev["set_disabled"]))
check("7d 连状态都没读（在护栏之前就返回了）", ev["is_disabled"] == [], str(ev["is_disabled"]))

print()
print("=" * 78)
print("8. 还原成功：写 IsDisabled=False + 回读为 False → 清 kick_until，返回 True")
print("=" * 78)
ns, ev = make_env(kick_until_set=True, is_disabled=False, restore_write_ok=True)
ok = restore(ns, source="到期")
row = list(ev["rows"].values())[0]
check("8a 返回 True", ok is True, str(ok))
check("8b 写了 IsDisabled=False", ev["set_disabled"] == [(EMBY, False)], str(ev["set_disabled"]))
check("8c 写入后回读校验了一次", ev["is_disabled"] == [EMBY], str(ev["is_disabled"]))
check("8d 回读确认后才清 kick_until",
      any(w.get("kick_until", "缺失") is None for w in ev["db"]), str(ev["db"]))
check("8e 数据库里 kick_until 已为 None", getattr(row, "kick_until", None) is None,
      str(row.__dict__))
check("8f 成功时不发群通知（不打扰管理员）", ev["announce"] == [], str(ev["announce"]))

# 8g/8h（2026-10-06 二轮）：Emby 侧已解封，但**清标记的写入失败**。
# 用户已经能看了，所以仍应返回 True；但绝不能像以前那样无论清没清成功都打印
# 「已还原并校验通过」的 info —— 必须如实记 warning（残留标记会让巡检多空跑一次）。
ns, ev = make_env(kick_until_set=True, is_disabled=False, restore_write_ok=True,
                  persist_ok=False)
ok = restore(ns, source="到期")
row = list(ev["rows"].values())[0]
check("8g 清标记写入失败 → 仍返回 True（用户确实已解封，这是关键事实）", ok is True, str(ok))
check("8h 清标记写入失败 → 记 warning 说明清除失败，且不得谎报 info「已还原并校验通过」",
      any(lvl == "warning" and "清除失败" in str(m) for lvl, m in ev["logs"])
      and not any(lvl == "info" and "已还原并校验通过" in str(m) for lvl, m in ev["logs"]),
      str(ev["logs"]))
check("8i 清标记写入失败 → 残留的 kick_until 仍在库里（下次巡检再清一次，幂等）",
      getattr(row, "kick_until", None) is not None, str(row.__dict__))

print()
print("=" * 78)
print("9. 还原失败：写入失败 / 回读仍为 True → 保留 kick_until + 群通知")
print("=" * 78)
for label, cfg in (("写入IsDisabled=False失败", dict(restore_write_ok=False)),
                   ("回读仍是IsDisabled=True", dict(is_disabled=True))):
    ns, ev = make_env(kick_until_set=True, **cfg)
    ok = restore(ns, source="到期")
    row = list(ev["rows"].values())[0]
    check(f"9a[{label}] 返回 False（没还原成功就不能说成功）", ok is False, str(ok))
    check(f"9b[{label}] 保留 kick_until：没有任何把它置 None 的写入",
          not any(w.get("kick_until", "缺失") is None for w in ev["db"]), str(ev["db"]))
    check(f"9c[{label}] 数据库里 kick_until 仍在（交给启动自恢复/周期巡检重试）",
          getattr(row, "kick_until", None) is not None, str(row.__dict__))
    check(f"9d[{label}] 发了群通知喊人（此刻用户还锁着）",
          len(ev["announce"]) == 1 and "还原失败" in ev["announce"][0], str(ev["announce"]))
    check(f"9e[{label}] 重试了 {ATTEMPTS} 次才放弃（不是一次就认输）",
          len(ev["set_disabled"]) == ATTEMPTS, str(ev["set_disabled"]))

print()
print("=" * 78)
print("10. 【接管护栏】kick_until 已被清空 → 一次都不能动 IsDisabled")
print("=" * 78)
ns, ev = make_env(kick_until_set=False)      # 行存在但 kick_until 为 None（已被 ban_user 接管）
ok = restore(ns, source="到期")
check("10a 返回 True（没有需要还原的东西，不算失败）", ok is True, str(ok))
check("10b 一次都没有调 set_user_disabled（否则会解开真正的封禁）",
      ev["set_disabled"] == [], str(ev["set_disabled"]))
check("10c 没有写库", ev["db"] == [], str(ev["db"]))
check("10d 没有发群通知（这不是异常）", ev["announce"] == [], str(ev["announce"]))

ns, ev = make_env(row_present=False)         # 连记录都没有
ok = restore(ns, source="启动自恢复")
check("10e 记录不存在时同样不动手（返回 True 且不写 IsDisabled）",
      ok is True and ev["set_disabled"] == [] and ev["db"] == [],
      f"ok={ok} set={ev['set_disabled']} db={ev['db']}")

print()
print("=" * 78)
print("10.5 【P2 回归】数据库**读失败** ≠ 已被接管")
print("=" * 78)
print("   `sql_get_emby` 在异常时也返回 None，而接管护栏把 None 解释成")
print("   「这个禁用已被别人接管」→ 直接返回成功。于是一次瞬时读失败会变成：")
print("   用户没被解封、restore_pending_kicks 却报 restored=1、main.py 还打印")
print("   「启动自恢复：已还原 N 个遗留的临时封禁」，而群里一条告警都没有。")
ns, ev = make_env(kick_until_set=True)       # 标记在、本该被还原
ev["db_read_raises"] = True                  # 但数据库这次读失败
ok = restore(ns, source="启动自恢复")
check("10f 读失败 → 返回 False（返回 True 就是「已还原」的假成功）", ok is False, str(ok))
check("10g 读失败 → 一次都没动 IsDisabled（用户可能还锁着，不能假装处理过）",
      ev["set_disabled"] == [], str(ev["set_disabled"]))
check("10h 读失败 → 没有清 kick_until（否则巡检再也找不到这条待还原记录）",
      not [d for d in ev["db"] if d.get("kick_until", "x") is None], str(ev["db"]))
check("10i 读失败 → 记 error 日志且口径点明是「读失败」",
      any(l == "error" and "读失败" in m for l, m in ev["logs"]),
      str(ev["logs"][-3:]))

print()
print("=" * 78)
print("11. ban_user 清标记：封禁成功才清，失败不清")
print("=" * 78)
ns, ev = make_env(ban_ok=True, kick_until_set=True)
r = asyncio.run(ns["ban_user"](EMBY, TG))
row = list(ev["rows"].values())[0]
check("11a 封禁成功返回 True", r is True, str(r))
check("11b 走的是 emby_change_policy(disable=True)",
      ev["policy"] == [(EMBY, True)], str(ev["policy"]))
check("11c 封禁成功 → 清掉 kick_until（声明「现在的禁用是终态」）",
      any(w.get("kick_until", "缺失") is None for w in ev["db"])
      and getattr(row, "kick_until", None) is None, str(ev["db"]))

ns, ev = make_env(ban_ok=False, kick_until_set=True)
r = asyncio.run(ns["ban_user"](EMBY, TG))
row = list(ev["rows"].values())[0]
check("11d 封禁失败返回 False", r is False, str(r))
check("11e 封禁失败 → 不清标记（清了就再也没人还原这个临时封禁）",
      not any(w.get("kick_until", "缺失") is None for w in ev["db"])
      and getattr(row, "kick_until", None) is not None, str(ev["db"]))

print()
print("=" * 78)
print("12. 启动自恢复：only_expired=False → 连**未到期**的也要还原（宁可少踢一次，不能锁死）")
print("=" * 78)
now = datetime.now(timezone.utc).replace(tzinfo=None)
future = now + timedelta(hours=2)
ns, ev = make_env(pending=[(TG, EMBY, future)], kick_until_set=True, kick_until_value=future,
                  is_disabled=False)
n = asyncio.run(ns["restore_pending_kicks"](only_expired=False))
row = list(ev["rows"].values())[0]
check("12a 返回还原条数 1（未到期的也还原了）", n == 1, str(n))
check("12b 真的写了 IsDisabled=False", (EMBY, False) in ev["set_disabled"], str(ev["set_disabled"]))
check("12c 清了 kick_until", getattr(row, "kick_until", None) is None, str(row.__dict__))

# 12d/12e：待还原记录缺少 embyid 的分支（无从还原 → 清标记避免每轮刷错误日志）
ns, ev = make_env(pending=[(TG, None, now - timedelta(hours=1))], kick_until_set=True,
                  is_disabled=False)
n = asyncio.run(ns["restore_pending_kicks"](only_expired=False))
check("12d 缺少 embyid → 不还原（返回 0）且不去动 IsDisabled",
      n == 0 and ev["set_disabled"] == [], f"n={n} set={ev['set_disabled']}")
check("12e 缺少 embyid → 清掉标记（否则每轮巡检都报同一条错）",
      any(w.get("kick_until", "缺失") is None for w in ev["db"])
      and any(lvl == "error" for lvl, _ in ev["logs"]), f"db={ev['db']} logs={ev['logs']}")

print()
print("=" * 78)
print("13. 周期巡检：only_expired=True → 未到期跳过、已到期还原")
print("=" * 78)
TG2, EMBY2 = 8638572039, "emby-other"
past = now - timedelta(hours=1)
row1 = Row(tg=TG, embyid=EMBY, name="a", lv="b", concurrent_warn_count=0, kick_until=future)
row2 = Row(tg=TG2, embyid=EMBY2, name="b", lv="b", concurrent_warn_count=0, kick_until=past)
ns, ev = make_env(rows={TG: row1, TG2: row2}, pending=[(TG, EMBY, future), (TG2, EMBY2, past)],
                  is_disabled=False)
n = asyncio.run(ns["restore_pending_kicks"](only_expired=True))
check("13a 只还原了 1 条（已到期的那个）", n == 1, str(n))
check("13b 未到期的 EMBY 完全没被动（不打断进行中的踢流）",
      (EMBY, False) not in ev["set_disabled"], str(ev["set_disabled"]))
check("13c 未到期的那条 kick_until 保持不变", row1.kick_until == future, str(row1.kick_until))
check("13d 已到期的 EMBY2 被还原", (EMBY2, False) in ev["set_disabled"], str(ev["set_disabled"]))
check("13e 已到期的那条 kick_until 被清", row2.kick_until is None, str(row2.kick_until))

print()
print("=" * 78)
print("14. _kick_restore_worker：正常到期要还原，**被取消也要还原**")
print("=" * 78)
ns, ev = make_env()


async def rec_restore(emby_id, tg_id, source):
    ev["restore_kick"].append((emby_id, tg_id, source))
    return True


ns["_restore_kick"] = rec_restore
ns["_kick_restore_worker"] = ns["_real_kick_restore_worker"]


async def cancel_scenario():
    t = asyncio.ensure_future(ns["_kick_restore_worker"](EMBY, TG, 999))
    await asyncio.sleep(0)          # 让任务真正进入 sleep
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        return True
    return False


raised = asyncio.run(cancel_scenario())
check("14a 任务被取消后确实抛出 CancelledError（异常语义未被吞掉）", raised is True, str(raised))
check("14b 被取消时仍然调用了 _restore_kick（否则进程退出会永久锁死用户）",
      len(ev["restore_kick"]) == 1, str(ev["restore_kick"]))
check("14c 取消路径的来源标注为「任务被取消」",
      bool(ev["restore_kick"]) and ev["restore_kick"][0][2] == "任务被取消",
      str(ev["restore_kick"]))

ns2, ev2 = make_env()


async def rec_restore2(emby_id, tg_id, source):
    ev2["restore_kick"].append((emby_id, tg_id, source))
    return True


ns2["_restore_kick"] = rec_restore2
ns2["_kick_restore_worker"] = ns2["_real_kick_restore_worker"]
asyncio.run(ns2["_kick_restore_worker"](EMBY, TG, 0))
check("14d 正常到期（hold=0）也调用 _restore_kick，来源为「到期」",
      ev2["restore_kick"] == [(EMBY, TG, "到期")], str(ev2["restore_kick"]))

print()
print("=" * 78)
print("15. 复验时机：走 kick 路径时 _schedule_stop_verify 的 wait 必须 > hold_seconds")
print("=" * 78)
ns, ev = make_env(warn_count=0, threshold=3, streams=3, limit=2, is_disabled=False)
asyncio.run(ns["check_concurrent_play_limit"]())
check("15a 确实走了 kick 路径（写了 IsDisabled=True）",
      (EMBY, True) in ev["set_disabled"], str(ev["set_disabled"]))
check("15b 复验已调度", len(ev["kick_sched"]) == 1, str(ev["kick_sched"]))
waits = [entry[2] for entry in ev["kick_sched"]]
check("15c wait 显式给出且严格大于 hold_seconds（否则复查到的是仍被禁用的假象）",
      bool(waits) and all(w is not None and w > HOLD for w in waits), f"wait={waits} hold={HOLD}")
# 2026-10-06 二轮：复验从「按旧 session id」改成「按 UserId」——因为解封后重连会
# 生成新的 session id，按旧 id 查必然查不到（无论有没有重连都报"已断开"，是假证据）。
check("15d 复验按 UserId 调度，带上流数，并传递阶梯通知档位",
      bool(ev["kick_sched"])
      and ev["kick_sched"][0][0] == EMBY and ev["kick_sched"][0][3] == 3
      and ev["kick_sched"][0][4] is False and ev["sched"] == [],
      f"kick_sched={ev['kick_sched']} 旧按id={ev['sched']}")

# 15e：会话连 Id 都拿不到时，**仍然**要调度复验 —— 这正是按 UserId 判定的意义：
# 它不需要旧 session id，所以"拿不到 id"不再是不能复验的理由。
ns, ev = make_env(warn_count=0, threshold=3, streams=3, limit=2, is_disabled=False,
                  session_ids=False)
asyncio.run(ns["check_concurrent_play_limit"]())
check("15e 拿不到会话 id 时仍按 UserId 调度复验（不依赖旧 id，故不受影响）",
      (EMBY, True) in ev["set_disabled"] and len(ev["kick_sched"]) == 1
      and ev["kick_sched"][0][0] == EMBY,
      f"set={ev['set_disabled']} kick_sched={ev['kick_sched']}")

# 15f：周期巡检本身抛异常时，本轮检测必须继续（安全网不能变成单点故障）
ns, ev = make_env(warn_count=0, threshold=3, streams=3, limit=2, is_disabled=False)


async def _boom(only_expired=False):
    raise RuntimeError("sweep boom")


ns["restore_pending_kicks"] = _boom
asyncio.run(ns["check_concurrent_play_limit"]())
check("15f 巡检抛异常 → 记录 error 但本轮检测照常执行（踢流仍然发生）",
      (EMBY, True) in ev["set_disabled"]
      and any(lvl == "error" for lvl, _ in ev["logs"]),
      f"set={ev['set_disabled']} logs={ev['logs']}")

# 15g（task-45）：踢流被拒（用户已被管理员禁用 → already_disabled）时走**回退路径**
# `_schedule_stop_verify`，回退路径也必须带上 emby_user_id —— 否则又退回
# "按旧 session id 复验"，客户端重连就会报假成功。
ns, ev = make_env(warn_count=0, threshold=3, streams=2, limit=1, is_disabled=True)
asyncio.run(ns["check_concurrent_play_limit"]())
check("15g 踢流被拒 → 回退调度带 emby_user_id 且首两次不发群复验",
      len(ev["sched"]) == 1 and ev["sched"][0][3] == EMBY
      and ev["sched"][0][4] is False and ev["kick_sched"] == [],
      f"sched={ev['sched']} kick_sched={ev['kick_sched']}")

print()
print("=" * 78)
print("16. ban 与 kick 互斥：达阈值只 ban，未达阈值只 kick")
print("=" * 78)
# 16A：warn_count=2, threshold=3 → +1=3 ≥ 3 → ban，不得再 kick
ns, ev = make_env(warn_count=2, threshold=3, streams=3, limit=2, is_disabled=False, ban_ok=True)
asyncio.run(ns["check_concurrent_play_limit"]())
check("16a 达阈值 → 走了 ban_user（emby_change_policy(disable=True)）",
      ev["policy"] == [(EMBY, True)], str(ev["policy"]))
check("16b 达阈值 → 绝不调 set_user_disabled（否则还原任务会解开刚下的封禁）",
      ev["set_disabled"] == [], str(ev["set_disabled"]))
check("16c 达阈值 → 不安排临时踢流（没有任何「设为非 None」的 kick_until 写入）",
      not any(w.get("kick_until") is not None for w in ev["db"]), str(ev["db"]))
check("16c2 达阈值 → ban_user 顺手把待还原标记清成 None（声明禁用是终态，与 kick 互斥）",
      any(w.get("kick_until", "缺失") is None for w in ev["db"]), str(ev["db"]))

# 16B：warn_count=0 → 1 < 3 → kick，不得 ban
ns, ev = make_env(warn_count=0, threshold=3, streams=3, limit=2, is_disabled=False, ban_ok=True)
asyncio.run(ns["check_concurrent_play_limit"]())
check("16d 未达阈值 → 走了 kick（set_user_disabled(True)）",
      ev["set_disabled"] == [(EMBY, True)], str(ev["set_disabled"]))
check("16e 未达阈值 → 绝不调 emby_change_policy（不越权封禁）",
      ev["policy"] == [], str(ev["policy"]))
check("16f 未达阈值 → 落了 kick_until",
      any("kick_until" in w for w in ev["db"]), str(ev["db"]))

print()
print("=" * 78)
print("17. 还原失败通报的节流判定（时间可伪造，不真等 1800 秒）")
print("=" * 78)
ns, ev = make_env()
alert = ns["_should_alert_restore_failure"]
clock = ev["clock"]
TG_OTHER = 8638572039

check("17a 首次失败（无任何记录）→ 允许通报", alert(TG, EMBY, "写入失败") is True)
check("17b 同用户 + 同原因 + 窗口内 → 抑制（不再通报）",
      alert(TG, EMBY, "写入失败") is False)
clock["now"] += timedelta(seconds=ALERT_INTERVAL - 1)
check("17c 窗口内差 1 秒仍抑制", alert(TG, EMBY, "写入失败") is False)
clock["now"] += timedelta(seconds=2)          # 累计已超过窗口
check("17d 超过窗口后同原因 → 再次通报", alert(TG, EMBY, "写入失败") is True)
check("17e 刚通报过又失败 → 再次抑制", alert(TG, EMBY, "写入失败") is False)
clock["now"] += timedelta(seconds=1)
check("17f 错误原因变化 → 立刻通报（不等窗口，那是新信息）",
      alert(TG, EMBY, "回读仍是禁用") is True)
check("17g 变化后的新原因也进入自己的窗口", alert(TG, EMBY, "回读仍是禁用") is False)
check("17h 另一个用户首次失败 → 独立通报（不受 TG 的窗口影响）",
      alert(TG_OTHER, EMBY, "写入失败") is True)
check("17i 另一个用户自己的窗口独立抑制", alert(TG_OTHER, EMBY, "写入失败") is False)
check("17j TG 的窗口不被 TG_OTHER 影响（旧原因仍在窗口内 → 抑制）",
      alert(TG, EMBY, "回读仍是禁用") is False)
check("17k 每个「用户 + embyid」一条记录：最后一次通报的（原因, 时间）",
      set(ns["_RESTORE_ALERTS"]) == {f"{TG}:{EMBY}", f"{TG_OTHER}:{EMBY}"},
      str(ns["_RESTORE_ALERTS"]))
check("17l tg_id 为 None 时不抛异常（key 里带 embyid）",
      alert(None, "emby-a", "写入失败") is True
      and alert(None, "emby-a", "写入失败") is False)
check("17m 【key 含 embyid】tg_id 都为 None 但 embyid 不同 → 各自独立，一条失败不抑制另一条",
      alert(None, "emby-b", "写入失败") is True)
check("17n 同一 embyid 才互相抑制（emby-b 抑制、emby-a 仍在自己的窗口内）",
      alert(None, "emby-b", "写入失败") is False
      and alert(None, "emby-a", "写入失败") is False)

# ── _clear_restore_alert：还原成功后必须能"重新开始计" ──
clear = ns["_clear_restore_alert"]
ns["_RESTORE_ALERTS"].clear()
check("17o 准备：首次失败允许通报", alert(TG, EMBY, "写入失败") is True)
check("17p 紧接着同原因 → 抑制（节流在工作）", alert(TG, EMBY, "写入失败") is False)
check("17q 【清理】清掉记录后同用户同原因立刻可再通报（下一次事故不被压掉）",
      clear(TG, EMBY) is None and alert(TG, EMBY, "写入失败") is True)
check("17r _clear_restore_alert 对不存在的 key 不抛异常（幂等，可无条件调用）",
      clear(TG, "不存在的-emby") is None and clear(None, None) is None)

print()
print("=" * 78)
print("18. 节流接到 _restore_kick 上：同原因连续失败 → 群通报 1 条、日志 2 条")
print("=" * 78)
ns, ev = make_env(kick_until_set=True, is_disabled=False, restore_write_ok=False, interval=0)
r1 = restore(ns)
r2 = restore(ns)
row = ev["rows"][TG]
errs = [m for lvl, m in ev["logs"] if lvl == "error" and "还原失败" in m]
check("18a 两次都返回 False（节流不改变「失败仍返回 False」的语义）",
      r1 is False and r2 is False, f"{r1}, {r2}")
check("18b 两次都真的重试了（各 %d 次写 IsDisabled=False）" % ATTEMPTS,
      ev["set_disabled"] == [(EMBY, False)] * ATTEMPTS * 2, str(ev["set_disabled"]))
check("18c 群通报只有 1 条（同一用户同一原因被节流）", len(ev["announce"]) == 1,
      str(ev["announce"]))
check("18d 但 LOGGER.error 记了 2 条（日志不受节流影响，排查时看得到每一次）",
      len(errs) == 2, str(errs))
_a0 = ev["announce"][0] if ev["announce"] else ""
check("18e 通报里写明错误原因与「仍处于禁用状态」",
      "写入 IsDisabled=False 失败" in _a0 and "仍处于禁用状态" in _a0,
      _a0[:200] or "没有发出通报")
check("18f 失败时 kick_until 仍保留（交给巡检继续重试，不静默放弃）",
      getattr(row, "kick_until", None) is not None, str(row.__dict__))

# 错误原因变化 → 立刻再通报（写入这次成功，但回读仍是禁用）
ns["emby"].state["restore_write_ok"] = True
ns["emby"].state["is_disabled"] = True
r3 = restore(ns)
errs = [m for lvl, m in ev["logs"] if lvl == "error" and "还原失败" in m]
check("18g 错误原因变化 → 立刻再通报一次（共 2 条）", len(ev["announce"]) == 2,
      str([a[:60] for a in ev["announce"]]))
_a1 = ev["announce"][1] if len(ev["announce"]) > 1 else ""
check("18h 通报里的原因是新的那个", "回读仍是 IsDisabled=True" in _a1,
      _a1[:200] or "没有发出第二条通报")
check("18i 第三次仍返回 False 且日志共 3 条", r3 is False and len(errs) == 3,
      f"r3={r3} errs={len(errs)}")

# 新原因也进入窗口 → 再次失败不再通报
r4 = restore(ns)
errs = [m for lvl, m in ev["logs"] if lvl == "error" and "还原失败" in m]
check("18j 新原因在窗口内再次失败 → 不再通报（仍 2 条），日志仍每次记（4 条）",
      len(ev["announce"]) == 2 and r4 is False and len(errs) == 4,
      f"announce={len(ev['announce'])} errs={len(errs)}")

# 超过窗口 → 同原因再次通报
ev["clock"]["now"] += timedelta(seconds=ALERT_INTERVAL + 1)
r5 = restore(ns)
check("18k 超过窗口后同原因 → 再次通报（3 条）", len(ev["announce"]) == 3 and r5 is False,
      f"announce={len(ev['announce'])} r5={r5}")

# 不同用户各自独立计
now = ev["clock"]["now"]
TG2, EMBY2 = 8638572039, "emby-other"
row1 = Row(tg=TG, embyid=EMBY, name="a", lv="b", concurrent_warn_count=0,
           kick_until=now + timedelta(seconds=60))
row2 = Row(tg=TG2, embyid=EMBY2, name="b", lv="b", concurrent_warn_count=0,
           kick_until=now + timedelta(seconds=60))
ns2, ev2 = make_env(rows={TG: row1, TG2: row2}, restore_write_ok=False, interval=0)
restore(ns2, EMBY, TG)
restore(ns2, EMBY, TG)
restore(ns2, EMBY2, TG2)
check("18l 不同用户的节流窗口互相独立（TG 被抑制时 TG2 仍通报）",
      len(ev2["announce"]) == 2, str([a[:50] for a in ev2["announce"]]))
_p0 = ev2["announce"][0] if ev2["announce"] else ""
_p1 = ev2["announce"][1] if len(ev2["announce"]) > 1 else ""
check("18m 两条通报分别属于两个用户",
      str(TG) in _p0 and str(TG2) in _p1,
      f"第一条含TG={str(TG) in _p0}, 第二条含TG2={str(TG2) in _p1}")

print()
print("=" * 78)
print("19. 还原成功后必须清节流：同一用户下一次事故（同原因、窗口内）仍要通报")
print("=" * 78)
# 这是 task-45 的 B.6，也是本轮最重要的一条：节流是为了压制「**同一次**事故被
# 周期巡检反复重试」的刷屏，不是压制新事故。若还原成功后不清记录，同一用户下一次
# 事故原因相同且落在 30 分钟窗口内 → 告警被当成"重复"抑制 → 群里一条 🚨 都没有，
# 而用户是真的又被锁在门外了。**第二次失败必须落在窗口内**，否则测不出这个 bug。
ns, ev = make_env(kick_until_set=True, is_disabled=False, restore_write_ok=False, interval=0)
row = ev["rows"][TG]
key = f"{TG}:{EMBY}"
t0 = ev["clock"]["now"]

r1 = restore(ns)
check("19a 第一次事故：还原失败 → 返回 False 且通报 1 条",
      r1 is False and len(ev["announce"]) == 1, f"r1={r1} announce={len(ev['announce'])}")
check("19b 节流记录已建立（key = 用户:embyid）", key in ns["_RESTORE_ALERTS"],
      str(ns["_RESTORE_ALERTS"]))

r2 = restore(ns)
check("19c 同一次事故的第二次失败（窗口内）→ 被节流，仍只有 1 条",
      r2 is False and len(ev["announce"]) == 1, f"r2={r2} announce={len(ev['announce'])}")

# 还原成功（这次写入成功且回读确认已解封）→ 必须清掉节流记录
ev["emby_state"]["restore_write_ok"] = True
r3 = restore(ns)
check("19d 还原成功 → 返回 True", r3 is True, str(r3))
check("19e ★ 成功时清掉了该用户的节流记录（否则下一次事故会被压掉）",
      key not in ns["_RESTORE_ALERTS"], str(ns["_RESTORE_ALERTS"]))
check("19f 成功时清了 kick_until（这次事故结束）", getattr(row, "kick_until", None) is None,
      str(row.__dict__))

# ★ 新一次事故：同一用户、**同一原因**、且**仍在 30 分钟窗口内** → 必须再通报
row.kick_until = t0 + timedelta(seconds=60)      # 新一次踢流重新落标记
ev["emby_state"]["restore_write_ok"] = False
elapsed = (ev["clock"]["now"] - t0).total_seconds()
r4 = restore(ns)
check("19g 新事故确实发生在节流窗口内（用伪造时钟，不真等）",
      elapsed < ALERT_INTERVAL, f"elapsed={elapsed}s 窗口={ALERT_INTERVAL}s")
check("19h ★【新事故】还原成功后、窗口内同原因再失败 → 必须再通报（共 2 条）",
      r4 is False and len(ev["announce"]) == 2,
      f"r4={r4} announce={len(ev['announce'])}（1 条 = 新事故被压掉了）")
check("19i 两条通报都是「还原失败」告警（不是别的消息凑数）",
      all("临时封禁还原失败" in a for a in ev["announce"]), str(ev["announce"])[:200])

print()
print("=" * 78)
print("20. 群里 @ 触发者（_mention）+ 临时封禁时长（用户明确要求 3 分钟）")
print("=" * 78)
_mention = ns["_mention"]
check("20a 有 tg_id → 文本提及 `[名字](tg://user?id=<id>)`（不依赖 username）",
      _mention(TG, "toe") == f"[toe](tg://user?id={TG})", str(_mention(TG, "toe")))
# 名字不转义的话，legacy Markdown 遇到 `_ * ` [ ]` 会让整条通报 400 发不出去
_weird = "a_b*c`d[e]f"
_m = _mention(TG, _weird)
check("20b 名字里的 Markdown 特殊字符被**真实** escape_markdown 转义（用真函数，不是 lambda）",
      _m == "[a\\_b\\*c\\`d\\[e\\]f](tg://user?id=%d)" % TG, repr(_m))
check("20c 转义结果里不含未转义的裸下划线/星号（否则整条消息会 400）",
      "\\_" in _m and "\\*" in _m and "\\[" in _m and "\\]" in _m and "\\`" in _m, repr(_m))
check("20d tg_id 为 None → 空串（绝不产生 tg://user?id=None 死链）",
      _mention(None, "toe") == "" and _mention(0, "toe") == "",
      f"{_mention(None, 'toe')!r} {_mention(0, 'toe')!r}")
check("20e 名字缺失时退回用 tg_id 当标签（不是 None 文本）",
      _mention(TG, None) == f"[{TG}](tg://user?id={TG})", str(_mention(TG, None)))

# 群通报必须真的带上 @（不是只在本函数里能生成）
ns, ev = make_env(warn_count=0, threshold=3, streams=3, limit=2, is_disabled=False)
asyncio.run(ns["check_concurrent_play_limit"]())
_first = ev["announce"][0] if ev["announce"] else ""
check("20f 第 1 次只发私聊，不发群消息", ev["announce"] == [] and len(ev["warn"]) == 1,
      f"群={ev['announce']} 私聊={ev['warn']}")

ns3rd, ev3rd = make_env(warn_count=2, threshold=3, streams=3, limit=2, is_disabled=False)
asyncio.run(ns3rd["check_concurrent_play_limit"]())
_first3 = ev3rd["announce"][0] if ev3rd["announce"] else ""
check("20g 第 3 次群通报含触发者文本提及和 TG 号",
      f"tg://user?id={TG}" in _first3 and "🔔 触发者" in _first3
      and f"（TG: `{TG}`）" in _first3, _first3[:240] or "没有通报")

# 没绑定 TG 的账号：@ 不了，必须**如实写明**，而不是静默留空
_rows = {None: Row(tg=None, embyid=EMBY, name="toe", lv="b", concurrent_warn_count=0)}
ns2, ev2 = make_env(warn_count=0, threshold=3, streams=3, limit=2, is_disabled=False,
                    rows=_rows)
asyncio.run(ns2["check_concurrent_play_limit"]())
_first2 = ev2["announce"][0] if ev2["announce"] else ""
check("20h 第 1 次未绑定 TG 仍只走私聊路径，不发群消息",
      ev2["announce"] == [] and ev2["warn"] == [], f"群={ev2['announce']} 私聊={ev2['warn']}")

# 时长：用户明确要求 3 分钟。这条**故意**硬编码 180 —— 它是需求本身，
# 改需求时就必须显式改这里；其余地方一律从 _KICK_HOLD_SECONDS 推导。
check("20j 临时封禁时长 = 180 秒（用户明确要求 3 分钟）", HOLD == 180, f"HOLD={HOLD}")
ns3, ev3 = make_env(warn_count=0, threshold=3, streams=3, limit=2, is_disabled=False)
asyncio.run(ns3["check_concurrent_play_limit"]())
_waits3 = [entry[2] for entry in ev3["kick_sched"]]
check("20k kick 路径的复验 wait 严格大于 hold_seconds（HOLD=%d 秒）" % HOLD,
      bool(_waits3) and all(w is not None and w > HOLD for w in _waits3),
      f"wait={_waits3} HOLD={HOLD}")
check("20l 落库的 kick_until ≈ now + HOLD（不是旧的 45 秒）",
      bool(ev3["db"]) and any(abs((v - ev3["clock"]["now"]).total_seconds() - HOLD) < 1
                              for d in ev3["db"] for k, v in d.items()
                              if k == "kick_until" and v is not None),
      str([(k, v) for d in ev3["db"] for k, v in d.items()]))

print()
print("=" * 78)
print(f"  结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
