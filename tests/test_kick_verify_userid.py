#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「按 UserId 复验」测试（task-40 B 部分）

背景（Lead 在 task-39 报告后修掉的真缺陷）：
`_verify_stop_followup()` 按**原 session id** 判断流有没有停。但用户解封后重连会
生成**新的 session id**，旧 id 永远不在列表里 → **无论用户有没有重连都会报"已断开"**，
那是假证据，恰好把"踢流没成功、用户又连回来了"这个真实失败掩盖掉。

修复后：`_user_sessions_playing()` 按 **UserId** 过滤，`_verify_kick_followup()`
据此报告「仍有 N 个流在播放（很可能重连）」或「当前没有任何播放流（未重连）」，
**取不到会话列表时报「未能确认」而不是 0**。

本套件用 AST 抽取真实函数 + 只替换 `emby._request`（网络边界），断言行为。

运行：python3 tests/test_kick_verify_userid.py
"""
import ast
import asyncio
import importlib.util
import sys
import textwrap
import types
from pathlib import Path

PASS, FAIL = 0, 0
REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "bot/modules/extra/concurrent_play_monitor.py"
SRC_TEXT = SRC.read_text(encoding="utf-8")
UID = "f58ac4d2b82341b492e7d5309a024b39"
OTHER = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def extract(name):
    for node in ast.walk(ast.parse(SRC_TEXT)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(SRC_TEXT, node))
    raise AssertionError(f"源码里找不到函数 {name}")


# ───────────────────────── 替身环境（只替换网络边界） ─────────────────────────

class Res:
    def __init__(self, ok, data=None, error=None):
        self.success, self.data, self.error = ok, data, error


SESSIONS_QUEUE = []     # 每次 GET /emby/Sessions 依次弹出的响应
STATE_QUEUE = []        # is_user_disabled 依次弹出的返回值（空则用 DEFAULT_DISABLED）
DEFAULT_DISABLED = False
STATE_CALLS = []        # is_user_disabled 收到的 emby_user_id
ORDER = []              # 调用顺序：("sessions", endpoint) / ("state", emby_id)
LOGS = []               # [(level, msg)]
API_CALLS = []
RAW_ENDPOINTS = []     # endpoint 原样保留
SESSIONS_LIST_RAW = [] # 只记"会话列表"端点，全程不清空（见文件末尾 6a~6c）
GROUP_MSGS = []


class _Logger:
    def _rec(self, lvl, m):
        LOGS.append((lvl, str(m)))

    def info(self, m, *a, **k): self._rec("info", m)
    def warning(self, m, *a, **k): self._rec("warning", m)
    def error(self, m, *a, **k): self._rec("error", m)
    def debug(self, m, *a, **k): self._rec("debug", m)


class _Cfg:
    concurrent_play_limit_enabled = True
    concurrent_play_limit = 2
    concurrent_play_limit_whitelist = 4
    concurrent_play_limit_whitelist_enabled = True
    concurrent_play_warn_threshold = 3


class _Emby:
    async def is_user_disabled(self, emby_id):
        """账号状态：由 STATE_QUEUE 驱动（空则 DEFAULT_DISABLED）。"""
        STATE_CALLS.append(emby_id)
        ORDER.append(("state", emby_id))
        if STATE_QUEUE:
            return STATE_QUEUE.pop(0)
        return DEFAULT_DISABLED

    async def _request(self, method, endpoint, **kw):
        # 生产代码现在用 `?ActiveWithinSeconds=300` 打会话接口（见
        # register_throttle.sessions_endpoint，避免每分钟拉回 4037 条僵尸会话）。
        # 这里把 query 归一化掉再记录/匹配，断言仍然只关心"打的是哪个接口"。
        API_CALLS.append((method, endpoint.split("?")[0]))
        RAW_ENDPOINTS.append(endpoint)
        if endpoint.split("?")[0] == "/emby/Sessions":
            SESSIONS_LIST_RAW.append(endpoint)
        if method == "GET" and endpoint.split("?")[0] == "/emby/Sessions":
            ORDER.append(("sessions", endpoint.split("?")[0]))
            if not SESSIONS_QUEUE:
                return Res(True, [])
            nxt = SESSIONS_QUEUE.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return Res(True, {})


def _install_stub_modules():
    bot_mod = types.ModuleType("bot")
    bot_mod.bot = object()
    bot_mod.group = [-1004344766463]
    bot_mod.config = _Cfg()
    bot_mod.LOGGER = _Logger()
    bot_mod.emby_url = "http://emby.test"
    bot_mod.emby_api = "key"
    bot_mod.emby_block = []
    bot_mod.extra_emby_libs = []
    emby_pkg = types.ModuleType("bot.func_helper.emby")
    emby_pkg.emby = _Emby()

    async def _not_admin(*a, **k):
        return False

    emby_pkg.is_emby_admin = _not_admin
    msg_utils = types.ModuleType("bot.func_helper.msg_utils")

    async def _sendMessage(*a, **k):
        pass

    msg_utils.sendMessage = _sendMessage
    # 真实 escape_markdown（_mention 依赖）。**不能**用 lambda 假替身：
    # 转义字符集是本套件要断言的行为之一。
    import html as _html, re as _re
    _mut_src = (REPO / "bot/func_helper/msg_utils.py").read_text(encoding="utf-8")
    _h = [n for n in ast.walk(ast.parse(_mut_src))
          if isinstance(n, ast.FunctionDef) and n.name == "escape_markdown"]
    assert len(_h) == 1, f"escape_markdown 命中 {len(_h)} 次"
    exec(compile(textwrap.dedent(ast.get_source_segment(_mut_src, _h[0])),
                 str(REPO / "bot/func_helper/msg_utils.py"), "exec"),
         {"re": _re, "html": _html, "escape_markdown": None}, msg_utils.__dict__)
    utils = types.ModuleType("bot.func_helper.utils")
    utils.judge_admins = lambda uid: False
    sql_emby = types.ModuleType("bot.sql_helper.sql_emby")

    class EmbyCol:
        def __eq__(self, o):
            return True

    class EmbyRow:
        tg = EmbyCol()
        embyid = EmbyCol()

    sql_emby.Emby = EmbyRow
    sql_emby.sql_get_emby = lambda **k: None
    sql_emby.sql_get_emby_checked = lambda **k: (None, True)
    sql_emby.sql_update_emby = lambda *a, **k: True
    sql_emby.sql_get_pending_kicks = lambda: []
    mods = [("bot", bot_mod), ("bot.func_helper", types.ModuleType("bot.func_helper")),
            ("bot.func_helper.emby", emby_pkg), ("bot.func_helper.msg_utils", msg_utils),
            ("bot.func_helper.utils", utils),
            ("bot.sql_helper", types.ModuleType("bot.sql_helper")),
            ("bot.sql_helper.sql_emby", sql_emby)]
    for name, mod in mods:
        sys.modules[name] = mod
    sys.modules["bot.func_helper"].emby = emby_pkg


_install_stub_modules()
_spec = importlib.util.spec_from_file_location("cpm", SRC)
cpm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpm)


async def _cap_group(text):
    GROUP_MSGS.append(text)


cpm.send_group_announcement = _cap_group


def sess(sid, user=UID, playing=True, item="剧A"):
    s = {"Id": sid, "UserId": user, "UserName": "toe", "Client": "CapyPlayer"}
    if playing:
        s["NowPlayingItem"] = {"Name": item}
    return s


def reset(*responses):
    SESSIONS_QUEUE.clear()
    STATE_QUEUE.clear()
    STATE_CALLS.clear()
    ORDER.clear()
    LOGS.clear()
    API_CALLS.clear()
    GROUP_MSGS.clear()
    SESSIONS_QUEUE.extend(responses)


def run(coro):
    return asyncio.run(coro)


print("=" * 78)
print("1. _user_sessions_playing：按 UserId 统计「当前仍在播放」的会话数")
print("=" * 78)

# ★ 假证据回归（单元级）：旧 id 全部消失，但该用户用**新 id** 重连了。
# 按旧 id 判定的实现会算出 0（→ 报"已断开"，假证据）；按 UserId 必须算出 1。
reset(Res(True, [sess("brand-new-session-9")]))
n = run(cpm._user_sessions_playing(UID))
check("1a 【假证据回归】旧 id 消失但有新 id 在播 → 必须统计到 1（不是 0）", n == 1, str(n))
check("1b 请求的是会话列表接口", API_CALLS == [("GET", "/emby/Sessions")], str(API_CALLS))

reset(Res(True, []))
check("1c 真的没有会话 → 0", run(cpm._user_sessions_playing(UID)) == 0)

reset(Res(True, [sess("s1"), sess("s2", item="剧B")]))
check("1d 两个新会话在播 → 2", run(cpm._user_sessions_playing(UID)) == 2)


print("2. 只统计「该用户」且「真正在播」的会话")
print("=" * 78)
reset(Res(True, [sess("a", user=OTHER), sess("b", user=OTHER)]))
check("2a 只有别人的流在播 → 该用户算 0（不得把别人算进来）",
      run(cpm._user_sessions_playing(UID)) == 0)
reset(Res(True, [sess("a", user=OTHER), sess("b", user=UID), sess("c", user=OTHER)]))
check("2b 混合场景 → 只数自己的那 1 个", run(cpm._user_sessions_playing(UID)) == 1)
reset(Res(True, [sess("a", playing=False), sess("b", playing=False)]))
check("2c 该用户连着但没在播（无 NowPlayingItem）→ 0",
      run(cpm._user_sessions_playing(UID)) == 0)
reset(Res(True, [{"Id": "a", "UserId": UID, "NowPlayingItem": None},
                 {"Id": "b", "UserId": UID, "NowPlayingItem": {}}]))
check("2d NowPlayingItem 为 None/空 dict → 视为没在播",
      run(cpm._user_sessions_playing(UID)) == 0)
reset(Res(True, [sess("a"), {"Id": "b", "NowPlayingItem": {"Name": "x"}}]))
check("2e 会话缺 UserId → 不得计入（不能靠「缺字段」蒙对）",
      run(cpm._user_sessions_playing(UID)) == 1)

print()
print("=" * 78)
print("3. 取不到会话列表 → 必须返回 None（不得谎报 0 个流）")
print("=" * 78)
reset(RuntimeError("network down"))
check("3a _request 抛异常 → None", run(cpm._user_sessions_playing(UID)) is None)
reset(Res(False, error="HTTP 503"))
check("3b success=False → None", run(cpm._user_sessions_playing(UID)) is None)
reset(Res(True, {"not": "a list"}))
check("3c data 不是 list → None", run(cpm._user_sessions_playing(UID)) is None)
reset(Res(True, None))
check("3d data 为 None → None", run(cpm._user_sessions_playing(UID)) is None)

print()
print("=" * 78)
print("4. _verify_kick_followup 的三种通报：必须有区别，且不得谎报")
print("=" * 78)
# 4A：解封后仍有流（重连）→ 报「仍有 N 个流在播放 / 很可能重连」
reset(Res(True, [sess("new-1"), sess("new-2", item="剧B")]))
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("4a 仍有流 → 通报含「仍有 2 个流在播放」", "仍有 2 个流在播放" in msg, msg[:160])
check("4b 仍有流 → 指出很可能是解封后重连并要求手动处理",
      "重连" in msg and "手动处理" in msg, msg[:160])
check("4c 仍有流 → 说明原有几个流（便于对账）", "原有 2 个" in msg, msg[:160])
check("4d 仍有流 → 绝不得说「没有任何播放流」/「未重连」",
      "没有任何播放流" not in msg and "未重连" not in msg, msg[:160])

# 4B：真的没重连 → 报「当前没有任何播放流（未重连）」
reset(Res(True, []))
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("4e 0 个流 → 通报含「当前没有任何播放流」", "当前没有任何播放流" in msg, msg[:160])
check("4f 0 个流 → 含「未重连」与「原有 2 个已断开」",
      "未重连" in msg and "原有 2 个已断开" in msg, msg[:160])
check("4g 0 个流 → 不得说「仍有」/「重连，请手动处理」",
      "仍有" not in msg and "很可能" not in msg, msg[:160])

# 4C：取不到 → 报「未能确认」，绝不当成 0 个流
for label, resp in (("抛异常", RuntimeError("boom")), ("success=False", Res(False, error="HTTP 503")),
                    ("不是 list", Res(True, "nope"))):
    reset(resp)
    run(cpm._verify_kick_followup(UID, "toe", 0, 2))
    msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
    check(f"4h[{label}] 取不到 → 通报含「未能确认」", "未能确认" in msg, msg[:160])
    check(f"4i[{label}] 取不到 → 绝不得说「没有任何播放流」（那是把「不知道」说成「没重连」）",
          "没有任何播放流" not in msg and "未重连" not in msg, msg[:160])

print()
print("=" * 78)
print("5. 复验必须先确认账号真的解封了（否则会发出自相矛盾的通报）")
print("=" * 78)

# ★ 矛盾消息回归（task-42 B.1）：还原失败 → 账号仍被禁用 → 该用户 0 个流。
# 不校验状态就报「✅ 账号已解封…未重连」，会和 _restore_kick 的
# 「🚨 还原失败…该用户当前仍处于禁用状态」同时在群里出现 —— 两条互相矛盾的消息。
reset(Res(True, []))
STATE_QUEUE.append(True)          # is_user_disabled → True（仍被禁用）
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("5a 账号仍被禁用 → 通报「仍处于禁用状态」", "仍处于禁用状态" in msg,
      msg[:200] or "没有发出通报")
check("5b 账号仍被禁用 → 要求管理员手动解封", "手动解封" in msg, msg[:200])
for bad in ("账号已解封", "没有任何播放流", "未重连"):
    check(f"5c 账号仍被禁用 → 绝不得出现「{bad}」（否则与还原失败告警自相矛盾）",
          bad not in msg, msg[:200])
check("5d 账号仍被禁用 → 记 error 级日志（这是必须有人介入的状态）",
      any(lvl == "error" and "仍被禁用" in m for lvl, m in LOGS), str(LOGS[-3:]))

# 即使列表里还有流，也不能报流数结论 —— 他根本连不上，"流数"没有意义
reset(Res(True, [sess("s1")]))
STATE_QUEUE.append(True)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("5e 账号仍被禁用（且列表里还有流）→ 仍只报禁用状态，不报「仍有 N 个流」",
      "仍处于禁用状态" in msg and "仍有" not in msg, msg[:200] or "没有发出通报")

# task-42 B.2：账号状态读不到 → 「未能确认」，不得说已解封、也不得下流数结论
reset(Res(True, []))
STATE_QUEUE.append(None)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("5f 状态读不到 → 通报「未能确认」", "未能确认" in msg, msg[:200] or "没有发出通报")
check("5g 状态读不到 → 绝不得说「账号已解封」", "账号已解封" not in msg, msg[:200])
check("5h 状态读不到 → 不得下流数结论（没有任何播放流 / 仍有 / 未重连）",
      all(b not in msg for b in ("没有任何播放流", "仍有", "未重连")), msg[:200])
check("5i 状态读不到 → 记 warning 级日志", any(lvl == "warning" for lvl, _ in LOGS),
      str(LOGS[-3:]))

# task-42 B.3：状态正常（False）时才进入原来的两个分支
reset(Res(True, []))
STATE_QUEUE.append(False)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("5j 状态正常 + 0 个流 → 仍报「当前没有任何播放流（未重连）」（原有行为不变）",
      "没有任何播放流" in msg and "未重连" in msg, msg[:200] or "没有发出通报")
reset(Res(True, [sess("n1")]))
STATE_QUEUE.append(False)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("5k 状态正常 + 1 个流 → 仍报「仍有 1 个流在播放 / 很可能重连」（原有行为不变）",
      "仍有 1 个流在播放" in msg and "重连" in msg, msg[:200] or "没有发出通报")

# task-45 B.1：调用顺序 —— 按**真实源码**断言（源码里 is_user_disabled 在
# _user_sessions_playing 之前），不是照抄任务描述猜的。
# 顺序为什么重要：账号状态与 /emby/Sessions 是**两个独立接口**。若先查会话而会话
# 接口恰好挂了，就会直接 return「未能确认」，把"用户被锁在门外"这个安全事故掩盖掉。
reset(Res(True, []))
STATE_QUEUE.append(False)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
check("5l 先查账号状态（is_user_disabled）再查流数（GET /emby/Sessions）",
      ORDER == [("state", UID), ("sessions", "/emby/Sessions")], str(ORDER))
check("5m 账号状态只查一次，且查的是同一个 emby_user_id", STATE_CALLS == [UID],
      str(STATE_CALLS))

# task-45 B.1（重点）：状态为 True / None 时**会话接口零调用** —— 用记录器断言，
# 不只看文案：少一次请求是次要的，关键是别让会话接口的故障影响安全结论。
reset(Res(True, []))
STATE_QUEUE.append(True)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("5n 账号仍被禁用 → 会话接口零调用（不被会话接口故障连累）",
      API_CALLS == [] and ORDER == [("state", UID)],
      f"api={API_CALLS} order={ORDER}")
check("5n2 账号仍被禁用 → 通报仍是禁用状态（安全事实优先于流数）",
      "仍处于禁用状态" in msg, msg[:160] or "没有发出通报")

reset(Res(True, []))
STATE_QUEUE.append(None)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("5n3 账号状态读不到 → 会话接口也零调用", API_CALLS == [] and ORDER == [("state", UID)],
      f"api={API_CALLS} order={ORDER}")
check("5n4 账号状态读不到 → 通报「未能确认是否已解封」", "未能确认" in msg,
      msg[:160] or "没有发出通报")

# task-45 B.2：状态 False（确实已解封）+ 会话接口取不到 → **新文案**：
# 先说清账号事实（已解封），再说清流数未能确认；不得报任何流数结论。
reset(RuntimeError("down"))
STATE_QUEUE.append(False)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
msg = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("5p 已解封但会话取不到 → 通报同时含「账号已解封」与「未能确认」",
      "账号已解封" in msg and "未能确认" in msg, msg[:200] or "没有发出通报")
check("5q 已解封但会话取不到 → 不得下流数结论（没有任何播放流 / 仍有 / 未重连）",
      all(b not in msg for b in ("没有任何播放流", "仍有", "未重连")), msg[:200])
check("5r 已解封但会话取不到 → 会话接口确实被调用了（状态为 False 才继续查流数）",
      API_CALLS == [("GET", "/emby/Sessions")], str(API_CALLS))
check("5s 已解封但会话取不到 → 记 warning 级日志", any(lvl == "warning" for lvl, _ in LOGS),
      str(LOGS[-3:]))

# 账号状态校验也必须发生在**下流数结论之前**：即使 0 个流，只要状态读不到就不许说
# 「未重连」（否则就是拿"不知道账号状态"去支撑一个流数结论）
reset(Res(True, []))
STATE_QUEUE.append(None)
run(cpm._verify_kick_followup(UID, "toe", 0, 2))
check("5o 状态读不到 + 0 个流 → 结论里不得出现「未重连」",
      "未重连" not in (GROUP_MSGS[-1] if GROUP_MSGS else ""),
      (GROUP_MSGS[-1] if GROUP_MSGS else "没有发出通报")[:200])

print()
print("=" * 78)
print("6. _schedule_kick_verify：起任务 + 持有强引用（避免被 GC）+ 参数透传")
print("=" * 78)


async def _case_schedule():
    seen = []
    gate = asyncio.Event()

    async def _pending(emby_user_id, user_name, wait, stream_count):
        seen.append((emby_user_id, user_name, wait, stream_count))
        await gate.wait()

    orig = cpm._verify_kick_followup
    cpm._verify_kick_followup = _pending
    try:
        task = cpm._schedule_kick_verify(UID, "toe", 60, 3)
        await asyncio.sleep(0)          # 让任务真正开始跑
        held = task in cpm._VERIFY_TASKS
        is_task = isinstance(task, asyncio.Task)
        gate.set()
        await task
        await asyncio.sleep(0)          # 让 done_callback 把强引用丢掉
        released = task not in cpm._VERIFY_TASKS
    finally:
        cpm._verify_kick_followup = orig
    return seen, is_task, held, released


seen, is_task, held, released = run(_case_schedule())
check("6a 返回一个 asyncio.Task", is_task)
check("6b 参数原样透传（emby_user_id / user_name / wait / stream_count）",
      seen == [(UID, "toe", 60, 3)], str(seen))
check("6c 任务未完成时被 _VERIFY_TASKS 持有强引用（否则可能被 GC 掉，复验就没了）", held)
check("6d 任务完成后从 _VERIFY_TASKS 释放（不泄漏）", released)

print()
print("=" * 78)
print("7. 环境自检：抽取的函数引用的全局在真实模块里都存在")
print("=" * 78)
for fn in ("_user_sessions_playing", "_verify_kick_followup", "_schedule_kick_verify",
           "_sessions_still_playing", "_verify_stop_followup"):
    check(f"7 {fn} 存在于真实模块中且可调用", callable(getattr(cpm, fn, None)))
def _referenced_globals(src):
    """引用但未在本地绑定的名字（要减掉赋值目标/参数/except as/import 别名）。"""
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
        elif isinstance(node, ast.Lambda):
            bound.update(a.arg for a in node.args.args)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - bound


_missing = sorted(n for n in _referenced_globals(
    "\n\n".join(extract(f) for f in ("_user_sessions_playing", "_verify_kick_followup")))
    if not hasattr(cpm, n) and n not in dir(__builtins__))
check("7b 两个被测函数引用的模块级名字都能在真实模块里解析到", _missing == [], str(_missing))
# ── 守住本次性能优化最关键的那个参数 ──────────────────────────────────────────
# 上面所有断言都把 endpoint 的 query 归一化掉了：好处是"打的是哪个接口"不受影响，
# 代价是如果有人把 `register_throttle.sessions_endpoint()` 改回裸端点，
# 这些断言仍然全绿，而线上会重新每分钟拉回 2.97 MB / 4037 条会话。
# 本套件不导入 bot 包（只有桩），所以直接从源码里把这个函数抽出来跑。
import ast as _ast
import os as _os

_THROTTLE_SRC = _os.path.join(REPO, "bot/func_helper/register_throttle.py")


def _sessions_endpoint_with(active_seconds):
    """抽出 sessions_endpoint() 的源码，用桩 config() 驱动，返回它生成的端点。"""
    tree = _ast.parse(open(_THROTTLE_SRC, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, _ast.FunctionDef) and n.name == "sessions_endpoint")
    ns = {"config": lambda: {"session_active_seconds": active_seconds}}
    exec(compile(_ast.Module(body=[fn], type_ignores=[]), "<throttle>", "exec"), ns)
    return ns["sessions_endpoint"]()


check("8a sessions_endpoint() 必须带 ActiveWithinSeconds 过滤（否则每小时多拉几百 MB）",
      _sessions_endpoint_with(300) == "/emby/Sessions?ActiveWithinSeconds=300",
      _sessions_endpoint_with(300))
check("8b 窗口值可配（120 → 120，0 → 退回裸端点）",
      _sessions_endpoint_with(120) == "/emby/Sessions?ActiveWithinSeconds=120"
      and _sessions_endpoint_with(0) == "/emby/Sessions",
      f"{_sessions_endpoint_with(120)} / {_sessions_endpoint_with(0)}")
check("8c 真实调用记录里也带上了这个参数（生产代码确实用了它）",
      bool(SESSIONS_LIST_RAW) and all("ActiveWithinSeconds=" in ep for ep in SESSIONS_LIST_RAW),
      str(SESSIONS_LIST_RAW))


print()
print("=" * 78)

print()
print("=" * 78)
print(f"结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 78)
sys.exit(0 if (FAIL == 0 and PASS > 0) else 1)
