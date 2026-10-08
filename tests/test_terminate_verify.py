#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
concurrent_play_monitor.py 终止流复验逻辑的单元测试。
用桩模块替代 bot 的真实依赖，不连 Emby、不连数据库。
"""
import asyncio, importlib.util, sys, types

import os
REPO = os.environ.get("WARN_TEST_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "bot/modules/extra/concurrent_play_monitor.py")

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [PASS] {name}")
    else:
        FAIL += 1; print(f"  [FAIL] {name}  {detail}")


# ─────────── 桩 ───────────
class Cfg:
    concurrent_play_limit_enabled = True
    concurrent_play_limit = 1
    concurrent_play_limit_whitelist = 4
    concurrent_play_limit_whitelist_enabled = True
    concurrent_play_warn_threshold = 3
    concurrent_play_check_interval = 60

class Res:
    def __init__(self, ok, data=None, error=None):
        self.success, self.data, self.error = ok, data, error

GROUP_MSGS = []       # send_group_announcement 捕获
DM_MSGS = []          # warn_user 捕获
API_CALLS = []        # (method, endpoint)，endpoint 已归一化掉 query
RAW_ENDPOINTS = []    # endpoint 原样保留（用来守住 ?ActiveWithinSeconds=300 这个参数）
SESSIONS_LIST_RAW = []  # 只记"会话列表"这个端点，且全程不清空（见文件末尾 6a~6c）
SESSIONS_QUEUE = []   # 每次 GET /emby/Sessions 依次返回的会话列表
EVENTS = []           # 副作用顺序：("persist", 字段) / ("disable", True|False)
VERIFY_SCHED = []     # _schedule_stop_verify 收到的 (ids, user_name, wait)
KICK_SCHED = []       # _schedule_kick_verify 收到的 (emby_user_id, user_name, wait, stream_count)

def make_emby():
    class E:
        async def _request(self, method, endpoint, **kw):
            # 会话端点现在带 `?ActiveWithinSeconds=300`（register_throttle.sessions_endpoint），
            # 记录时归一化掉 query，断言仍然只关心接口本身。
            API_CALLS.append((method, endpoint.split("?")[0]))
            RAW_ENDPOINTS.append(endpoint)
            if endpoint.split("?")[0] == "/emby/Sessions":
                SESSIONS_LIST_RAW.append(endpoint)
            if method == "GET" and endpoint.split("?")[0] == "/emby/Sessions":
                return SESSIONS_QUEUE.pop(0) if SESSIONS_QUEUE else Res(True, [])
            if "Playing/Stop" in endpoint:
                return Res(True, b"")
            if endpoint.endswith("/Message"):
                return Res(True, b"")
            return Res(True, {})
        async def emby_change_policy(self, emby_id, admin=False, disable=False):
            return True
        async def is_user_disabled(self, emby_id):
            # 用户本来是启用的 → 允许走新的「临时封禁踢流」路径
            return False
        async def set_user_disabled(self, emby_id, disabled, before_write=None):
            # 真实实现把「落库」放在**锁内、POST 之前**（before_write 回调），
            # 顺序必须是 persist → disable，替身也照这个顺序记。
            if before_write is not None:
                if not await before_write():
                    EVENTS.append(("before_write_failed", disabled))
                    return False
            EVENTS.append(("disable", disabled))
            return True
    return E()

emby_stub = make_emby()

bot_mod = types.ModuleType("bot")
bot_mod.bot = object()
bot_mod.group = [-1004344766463]
bot_mod.config = Cfg()
LOGS = []          # [(level, msg)] —— 复验日志要能断言口径（按用户 / 按会话 ID）

class L:
    def _rec(self, lvl, m): LOGS.append((lvl, str(m)))
    def info(self, m, *a, **k): self._rec("info", m)
    def warning(self, m, *a, **k): self._rec("warning", m)
    def error(self, m, *a, **k): self._rec("error", m)
    def debug(self, m, *a, **k): self._rec("debug", m)
bot_mod.LOGGER = L()

emby_pkg = types.ModuleType("bot.func_helper.emby")
emby_pkg.emby = emby_stub
async def _not_admin(*a, **k): return False
emby_pkg.is_emby_admin = _not_admin

msg_utils = types.ModuleType("bot.func_helper.msg_utils")
async def _sendMessage(*a, **k): pass
msg_utils.sendMessage = _sendMessage
# concurrent_play_monitor 现在 `from bot.func_helper.msg_utils import sendMessage,
# escape_markdown`。**不能**桩成 lambda s: s —— 那会让"名字里的 Markdown 特殊
# 字符被转义"失去意义。这里从真实源码抽出纯函数挂上去。
import ast as _ast_early, html as _html_early, re as _re_early, textwrap as _tw_early
_MSG_UTILS_SRC = (os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                  + "/bot/func_helper/msg_utils.py")
_MUT = open(_MSG_UTILS_SRC, encoding="utf-8").read()
_hits = [n for n in _ast_early.walk(_ast_early.parse(_MUT))
         if isinstance(n, _ast_early.FunctionDef) and n.name == "escape_markdown"]
assert len(_hits) == 1, f"escape_markdown 命中 {len(_hits)} 次"
exec(compile(_tw_early.dedent(_ast_early.get_source_segment(_MUT, _hits[0])),
             _MSG_UTILS_SRC, "exec"),
     {"re": _re_early, "html": _html_early, "escape_markdown": None},
     msg_utils.__dict__)

utils = types.ModuleType("bot.func_helper.utils")
utils.judge_admins = lambda uid: False

sql_emby = types.ModuleType("bot.sql_helper.sql_emby")
class EmbyCol:
    def __eq__(self, o): return True
class EmbyRow:
    tg = EmbyCol()
sql_emby.Emby = EmbyRow
sql_emby.sql_get_emby = lambda **k: None
# 真实 `_restore_kick` 用三态版本（区分「查不到」与「查失败」）；这里按"读成功"给。
sql_emby.sql_get_emby_checked = lambda **k: (None, True)
sql_emby.sql_update_emby = lambda *a, **k: None
# check_concurrent_play_limit 现在每轮先做一次「到期未还原的临时封禁」巡检，
# 它内部 `from bot.sql_helper.sql_emby import sql_get_pending_kicks`。补上这个
# 桩，让真实巡检代码能跑（返回空 = 没有待还原记录），而不是靠异常被吞掉。
sql_emby.sql_get_pending_kicks = lambda: []

for name, mod in [("bot", bot_mod), ("bot.func_helper", types.ModuleType("bot.func_helper")),
                  ("bot.func_helper.emby", emby_pkg), ("bot.func_helper.msg_utils", msg_utils),
                  ("bot.func_helper.utils", utils), ("bot.sql_helper", types.ModuleType("bot.sql_helper")),
                  ("bot.sql_helper.sql_emby", sql_emby)]:
    sys.modules[name] = mod
sys.modules["bot.func_helper"].emby = emby_pkg

spec = importlib.util.spec_from_file_location("cpm", SRC)
cpm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpm)

# 把群通报/私信换成捕获
async def cap_group(text): GROUP_MSGS.append(text)
async def cap_dm(tg, text): DM_MSGS.append(text)
cpm.send_group_announcement = cap_group
cpm.warn_user = cap_dm
cpm._STOP_VERIFY_FOLLOWUP = 0     # 测试里不真等 30 秒

# 复验调度：走「临时封禁踢流」路径时，真实实现刻意把 wait 设成
# `hold_seconds + 15`（=60 秒，晚于解封），测试里不可能真等 60 秒。
# 这里换成「记录 wait + 立刻以 wait=0 跑一遍真实复验」的替身：既保留
# 「首报 + 复验」两条消息的端到端行为，又能断言调度时机的语义（见测试 4）。
def cap_schedule(session_ids, user_name, wait=None, emby_user_id=None):
    VERIFY_SCHED.append((list(session_ids), user_name, wait, emby_user_id))
    # 记录之后把**真实**复验以 wait=0 跑一遍：这样"回退路径是否按 UserId 复验"
    # 有端到端证据，而不是只看调度参数。
    asyncio.ensure_future(
        cpm._verify_stop_followup(list(session_ids), user_name, wait=0, emby_user_id=emby_user_id)
    )
    return asyncio.ensure_future(cpm._verify_stop_followup(session_ids, user_name, wait=0))
cpm._schedule_stop_verify = cap_schedule

# 2026-10-06 二轮：kick 路径改用**按 UserId** 的复验（`_verify_kick_followup`），
# 不再用按旧 session id 的 `_verify_stop_followup`。同样换成「记录参数 + 立刻以
# wait=0 跑一遍真实复验」的替身，这样端到端仍然会产出「首报 + 复验」两条消息。
def cap_kick_schedule(emby_user_id, user_name, wait, stream_count):
    KICK_SCHED.append((emby_user_id, user_name, wait, stream_count))
    return asyncio.ensure_future(
        cpm._verify_kick_followup(emby_user_id, user_name, wait=0, stream_count=stream_count)
    )
cpm._schedule_kick_verify = cap_kick_schedule


def sess(sid, name="片名", user="toe"):
    return {"Id": sid, "UserId": "f58ac4d2b82341b492e7d5309a024b39", "UserName": user,
            "Client": "CapyPlayer", "NowPlayingItem": {"Name": name}}

def run(coro): return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


print("=" * 70)
print("测试 1：terminate_all_user_sessions 返回值语义")
print("=" * 70)
API_CALLS.clear(); GROUP_MSGS.clear()
acc, rej, ids = asyncio.run(cpm.terminate_all_user_sessions("uid1", [sess("s1"), sess("s2")]))
check("两个都接受 → accepted=2", acc == 2, f"实际 {acc}")
check("rejected=0", rej == 0, f"实际 {rej}")
check("返回 accepted_ids 供复验", ids == ["s1", "s2"], f"实际 {ids}")
check("对每个会话都发了 Stop",
      sum(1 for m, e in API_CALLS if "Playing/Stop" in e) == 2)
check("对每个会话都发了 Message",
      sum(1 for m, e in API_CALLS if e.endswith("/Message")) == 2)

# 服务端拒绝一个
API_CALLS.clear()
orig = cpm.emby._request
calls = {"n": 0}
async def flaky(method, endpoint, **kw):
    if "Playing/Stop" in endpoint:
        calls["n"] += 1
        return Res(True, b"") if calls["n"] == 1 else Res(False, error="HTTP 500")
    return await orig(method, endpoint, **kw)
cpm.emby._request = flaky
acc, rej, ids = asyncio.run(cpm.terminate_all_user_sessions("uid1", [sess("s1"), sess("s2")]))
check("一个被拒 → accepted=1, rejected=1", (acc, rej) == (1, 1), f"实际 {(acc, rej)}")
check("accepted_ids 只含被接受的", ids == ["s1"], f"实际 {ids}")
cpm.emby._request = orig


print("测试 2：_sessions_still_playing —— 取不到时必须返回 None（不谎报成功）")
print("=" * 70)
SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(True, [sess("s1"), sess("s2")]))
r = asyncio.run(cpm._sessions_still_playing({"s1", "s2"}))
check("都在播 → 返回两个", r == {"s1", "s2"}, f"实际 {r}")

SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(True, [sess("s1")]))       # s2 已停
r = asyncio.run(cpm._sessions_still_playing({"s1", "s2"}))
check("只剩一个在播 → 返回 {s1}", r == {"s1"}, f"实际 {r}")

SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(False, error="HTTP 503"))
r = asyncio.run(cpm._sessions_still_playing({"s1"}))
check("API 失败 → 返回 None（无法确认）", r is None, f"实际 {r}")

SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(True, {"not": "a list"}))
r = asyncio.run(cpm._sessions_still_playing({"s1"}))
check("返回非列表 → 返回 None", r is None, f"实际 {r}")

# 会话存在但已不播放 → 不算 still
SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(True, [{"Id": "s1", "UserName": "toe"}]))   # 无 NowPlayingItem
r = asyncio.run(cpm._sessions_still_playing({"s1"}))
check("会话还在但没在播 → 视为已停（返回空集）", r == set(), f"实际 {r}")

print()
print("=" * 70)
print("测试 3：_verify_stop_followup 的三种结果文案")
print("=" * 70)
GROUP_MSGS.clear(); SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(True, []))                 # 全断开
asyncio.run(cpm._verify_stop_followup(["s1", "s2"], "toe", wait=0))
# 注意：这里一律先判"有没有发出通报"再取 [-1] —— 否则一旦实现没发消息，
# 套件会以 IndexError 崩在汇总之前，把"断言失败"变成"没有判据"。
_last = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("全断开 → 文案含『原会话已全部断开』（不再说成确证过）",
      "原会话已全部断开" in _last, _last[:160] or "没有发出通报")
check("全断开 → 必须写明该口径的局限：『若他在此期间重连则会漏判』",
      "重连则会漏判" in _last, _last[:200] or "没有发出通报")
check("全断开 → 数字为 2/2", "2/2" in _last, _last[:80] or "没有发出通报")

GROUP_MSGS.clear(); SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(True, [sess("s1")]))       # 还剩一个
asyncio.run(cpm._verify_stop_followup(["s1", "s2"], "toe", wait=0))
_last = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("部分未断 → 含『仍在播放』", "仍在播放" in _last, _last[:120] or "没有发出通报")
check("部分未断 → 已断开=1", "**1**" in _last, _last[:120] or "没有发出通报")

GROUP_MSGS.clear(); SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(False, error="boom"))      # 取不到
asyncio.run(cpm._verify_stop_followup(["s1"], "toe", wait=0))
_last = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("取不到 → 文案含『未能确认』", "未能确认" in _last, _last[:120] or "没有发出通报")

print()
print("=" * 70)
print("测试 3B：_verify_stop_followup 优先按 UserId（唯一能抓住重连的口径）")
print("=" * 70)
EMBY_ID = "f58ac4d2b82341b492e7d5309a024b39"

# (a) 按 UserId：0 个流 → 未重连
GROUP_MSGS.clear(); SESSIONS_QUEUE.clear(); LOGS.clear()
SESSIONS_QUEUE.append(Res(True, []))
run(cpm._verify_stop_followup(["s1", "s2"], "toe", wait=0, emby_user_id=EMBY_ID))
_last = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("3B-a 按 UserId、0 个流 → 『该用户当前没有任何播放流』且『未重连』",
      "当前没有任何播放流" in _last and "未重连" in _last,
      _last[:200] or "没有发出通报")
check("3B-a 说明本轮已断开几个（可对账）", "本轮已断开 2 个" in _last, _last[:200])
check("3B-a 日志口径写明『按用户复验』",
      any("按用户复验" in m for _l, m in LOGS), str(LOGS[-3:]))

# (b) ★ 假证据回归：旧 session id 消失，但该用户仍有 1 个**新 id** 的流在播。
#     按 session id 判定的实现会算出 0 → 报"已全部断开"（假证据）。
GROUP_MSGS.clear(); SESSIONS_QUEUE.clear(); LOGS.clear()
SESSIONS_QUEUE.append(Res(True, [sess("brand-new", "重连的剧")]))
run(cpm._verify_stop_followup(["s1", "s2"], "toe", wait=0, emby_user_id=EMBY_ID))
_last = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("3B-b 【假证据回归】旧 id 消失但有新 id 在播 → 必须报『仍有 1 个流在播放』",
      "仍有 1 个流在播放" in _last, _last[:200] or "没有发出通报")
check("3B-b 说明停止指令未生效、要求手动处理",
      "停止指令未能生效" in _last and "手动处理" in _last, _last[:200])
check("3B-b 绝不得报『已全部断开』/『没有任何播放流』/『未重连』",
      not any(w in _last for w in ("已全部断开", "没有任何播放流", "未重连")), _last[:200])

# (c) 按 UserId 但会话接口取不到 → 未能确认（不谎报）
GROUP_MSGS.clear(); SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(False, error="HTTP 503"))
run(cpm._verify_stop_followup(["s1", "s2"], "toe", wait=0, emby_user_id=EMBY_ID))
_last = GROUP_MSGS[-1] if GROUP_MSGS else ""
check("3B-c 按 UserId 但取不到 → 『未能确认』，不得报已停止",
      "未能确认" in _last and "已全部断开" not in _last, _last[:200] or "没有发出通报")

# (d) 不传 emby_user_id → 退回会话 ID 口径，日志必须写明口径
GROUP_MSGS.clear(); SESSIONS_QUEUE.clear(); LOGS.clear()
SESSIONS_QUEUE.append(Res(True, []))
run(cpm._verify_stop_followup(["s1", "s2"], "toe", wait=0))
check("3B-d 退回口径时日志写明『按会话 ID 复验』（口径可追溯）",
      any("按会话 ID 复验" in m for _l, m in LOGS), str(LOGS[-3:]))


print()
print("=" * 70)
print("测试 4：端到端 —— check_concurrent_play_limit 的文案必须诚实")
print("=" * 70)

# 造一个超限用户：数据库里能查到、非管理员、非白名单
class Row:
    tg = 1156115326
    embyid = "f58ac4d2b82341b492e7d5309a024b39"
    name = "toe"
    lv = "b"
    concurrent_warn_count = 0
cpm.sql_get_emby = lambda **k: Row()
def _sql_update(where, **kw):
    # 真实实现返回 bool：kick_user_streams 用它判断「落库是否成功」，
    # 返回 None 会被当成落库失败而拒绝禁用，所以这里必须如实返回 True。
    EVENTS.append(("persist", tuple(sorted(kw))))
    return True
cpm.sql_update_emby = _sql_update
cpm.judge_admins = lambda uid: False

async def main():
    GROUP_MSGS.clear(); DM_MSGS.clear(); SESSIONS_QUEUE.clear(); API_CALLS.clear()
    # 第 1 次 GET /emby/Sessions（检测用）：两个流
    SESSIONS_QUEUE.append(Res(True, [sess("s1", "剧A"), sess("s2", "剧B")]))
    # 第 2 次（复验用）：都还在播 → 模拟"204 但客户端没断"
    SESSIONS_QUEUE.append(Res(True, [sess("s1"), sess("s2")]))
    await cpm.check_concurrent_play_limit()
    await asyncio.sleep(0.2)      # 让后台复验任务跑完

asyncio.run(main())
print("  ── 群通报内容 ──")
for m in GROUP_MSGS: print("   " + m.replace("\n", "\n   "))
print("  ── 私信内容 ──")
for m in DM_MSGS: print("   " + m.replace("\n", "\n   "))

check("群通报共 2 条（首报 + 复验）", len(GROUP_MSGS) == 2, f"实际 {len(GROUP_MSGS)}")
first = GROUP_MSGS[0] if GROUP_MSGS else ""
check("首报用『已下发停止指令』而非『已终止』", "已下发停止指令: 2 个流" in first)
check("首报不再出现『已终止:』", "已终止:" not in first)
# 旧断言断言的是被删掉的旧文案（「复验结果将在 30 秒后补发」）。新实现刻意**不再**
# 承诺"30 秒后复验"：走临时封禁踢流时用户要到 hold_seconds 之后才解封，30 秒时
# 他其实"还禁着"，那时看到的"已断开"证明不了解封后没重连。所以这里改断言**语义**：
#   ① 说清实际做了什么（已下发指令 + 临时禁用踢流）；② 不谎称已终止/已断开；
#   ③ 复验调度时间必须晚于解封时刻（这才是"复验有意义"的前提）。
check("首报点明『已下发停止指令』且说明本服务器客户端通常不支持（不夸大效果）",
      "已下发停止指令" in first and "通常无效" in first, first[:400])
check("首报说明实际动作是『临时禁用该账号』且到期自动解封（诚实交代代价与恢复）",
      "临时禁用" in first and "到期自动解封" in first, first[:400])
check("首报不谎称流已被终止/已断开",
      not any(w in first for w in ("已终止", "已被强制终止", "已断开")), first[:400])
check("首报 @ 到触发者本人（文本提及，不依赖 username）",
      "tg://user?id=1156115326" in first and "🔔 触发者" in first, first[:240])
check("首报带上 TG 号便于人工核对", "（TG: `1156115326`）" in first, first[:240])
kick_sched = list(KICK_SCHED)
# 时长从源码常量推导，不硬编码 45/180（下次调整需求时这里不该再红一片）
HOLD = cpm._KICK_HOLD_SECONDS
check(f"复验按 UserId 调度（不是按旧 session id），且 wait 晚于解封时刻（> {HOLD}s）",
      len(kick_sched) == 1
      and kick_sched[0][0] == "f58ac4d2b82341b492e7d5309a024b39"
      and kick_sched[0][2] > HOLD and kick_sched[0][3] == 2
      and VERIFY_SCHED == [],
      f"kick_sched={kick_sched} 旧按id调度={VERIFY_SCHED}")
kick_events = [e for e in EVENTS
               if e[0] == "disable" or (e[0] == "persist" and "kick_until" in e[1])]
check("落库 kick_until 发生在写 IsDisabled=True 之前（顺序不可颠倒）",
      kick_events == [("persist", ("kick_until",)), ("disable", True)], f"{EVENTS}")
second = GROUP_MSGS[1] if len(GROUP_MSGS) > 1 else ""
check("复验报实测结果：仍有 2 个流在播放（不是 0、也不说已断开）",
      "仍有 2 个流在播放" in second, second[:160])
check("复验指出很可能是解封后重连、要求手动处理",
      "重连" in second and "手动处理" in second, second[:160])
dm = DM_MSGS[0] if DM_MSGS else ""
check("私信不谎称已终止", "所有播放流已被强制终止" not in dm)
check(f"私信说明账号被临时禁用、{HOLD} 秒后自动解封（诚实告知代价与恢复）",
      "临时禁用" in dm and str(HOLD) in dm and "自动解封" in dm, dm[:400])

# 场景二：复验时确实都断了
async def main2():
    GROUP_MSGS.clear(); DM_MSGS.clear(); SESSIONS_QUEUE.clear()
    SESSIONS_QUEUE.append(Res(True, [sess("s1"), sess("s2")]))
    SESSIONS_QUEUE.append(Res(True, []))     # 复验时已全部断开
    await cpm.check_concurrent_play_limit()
    await asyncio.sleep(0.2)
asyncio.run(main2())
check("复验确实 0 个流 → 文案『当前没有任何播放流』且『未重连』",
      len(GROUP_MSGS) == 2 and "当前没有任何播放流" in GROUP_MSGS[1]
      and "未重连" in GROUP_MSGS[1] and "原有 2 个已断开" in GROUP_MSGS[1],
      GROUP_MSGS[1][:160] if len(GROUP_MSGS) > 1 else "无第二条")

# 场景三：GET 失败 → 不谎报
async def main3():
    GROUP_MSGS.clear(); SESSIONS_QUEUE.clear()
    SESSIONS_QUEUE.append(Res(True, [sess("s1"), sess("s2")]))
    SESSIONS_QUEUE.append(Res(False, error="HTTP 503"))
    await cpm.check_concurrent_play_limit()
    await asyncio.sleep(0.2)
asyncio.run(main3())
check("复验取不到 → 『未能确认』，不说已停止",
      len(GROUP_MSGS) == 2 and "未能确认" in GROUP_MSGS[1],
      GROUP_MSGS[1][:120] if len(GROUP_MSGS) > 1 else "无第二条")

# ── 场景四（★ 本轮重点：假证据回归）────────────────────────────────────────
# 解封后用户**重连**了，新会话带的是**新的 session id**。
# 此时旧 id（s1/s2）确实已从会话列表里消失，但该用户仍有 1 个新 id 的流在播。
# 按旧 id 判定的实现会算出 0 个 → 报「已确认全部断开」—— 那是**假证据**，
# 恰好把"踢流没成功、用户又连回来了"这个真实失败掩盖掉。
# 按 UserId 判定的实现必须报「仍有 1 个流在播放（很可能重连）」。
async def main4():
    GROUP_MSGS.clear(); DM_MSGS.clear(); SESSIONS_QUEUE.clear()
    SESSIONS_QUEUE.append(Res(True, [sess("s1"), sess("s2")]))   # 检测：2 个流
    SESSIONS_QUEUE.append(Res(True, [sess("s9", "重连的剧")]))    # 复验：旧 id 没了，新 id 在播
    await cpm.check_concurrent_play_limit()
    await asyncio.sleep(0.2)

asyncio.run(main4())
print("  ── 复验通报（解封后重连）──")
for m in GROUP_MSGS[1:]:
    print("   " + m.replace("\n", "\n   "))
second = GROUP_MSGS[1] if len(GROUP_MSGS) > 1 else ""
check("【假证据回归】旧 id 消失但有新 id 在播 → 必须报『仍有 1 个流在播放』",
      "仍有 1 个流在播放" in second, second[:200])
check("【假证据回归】必须指出很可能重连、要求手动处理",
      "重连" in second and "手动处理" in second, second[:200])
check("【假证据回归】绝不得报『没有任何播放流』/『已确认全部断开』/『未重连』",
      not any(w in second for w in ("没有任何播放流", "已确认全部断开", "未重连")), second[:200])

print()
print("=" * 70)
print("测试 5：回退路径（踢流失败但有停止指令）也必须按 UserId 复验")
print("=" * 70)
# 造一个"已被管理员禁用"的用户 → kick_user_streams 会拒绝禁用（避免抢管理员的封禁）
# → kicked=False，但停止指令已下发 → 走 `elif accepted_ids: _schedule_stop_verify(...)`
# 这条回退路径也必须带上 emby_user_id，否则又退回"按旧 session id 报已断开"的假证据。
async def main5():
    GROUP_MSGS.clear(); DM_MSGS.clear(); SESSIONS_QUEUE.clear()
    VERIFY_SCHED.clear(); KICK_SCHED.clear()
    emby_stub.is_user_disabled = lambda emby_id: _true()
    SESSIONS_QUEUE.append(Res(True, [sess("s1"), sess("s2")]))       # 检测：2 个流
    SESSIONS_QUEUE.append(Res(True, [sess("new-9", "重连的剧")]))     # 复验：旧 id 没了、新 id 在播
    await cpm.check_concurrent_play_limit()
    await asyncio.sleep(0.3)

async def _true():
    return True

run(main5())
check("5a 踢流被拒（用户已被禁用）→ 走回退路径调度复验", len(VERIFY_SCHED) == 1,
      str(VERIFY_SCHED))
check("5b 回退路径也把 emby_user_id 传给复验（否则又会按旧 id 报假证据）",
      bool(VERIFY_SCHED) and VERIFY_SCHED[0][3] == EMBY_ID, str(VERIFY_SCHED))
second = GROUP_MSGS[1] if len(GROUP_MSGS) > 1 else ""
check("5c 回退路径的复验仍按 UserId 判定：报『仍有 1 个流在播放』而不是『已全部断开』",
      "仍有 1 个流在播放" in second and "已全部断开" not in second,
      second[:200] or "无第二条通报")

# ── 守住本次性能优化最关键的那个参数 ──────────────────────────────────────────
# 上面所有断言都把 endpoint 的 query 归一化掉了：好处是"打的是哪个接口"不受影响，
# 代价是如果有人把 `register_throttle.sessions_endpoint()` 改回裸端点，
# 这些断言仍然全绿，而线上会重新每分钟拉回 2.97 MB / 4037 条会话。
# 本套件不导入 bot 包（只有桩），所以直接从源码里把这个函数抽出来跑。
import ast as _ast

_THROTTLE_SRC = os.path.join(REPO, "bot/func_helper/register_throttle.py")


def _sessions_endpoint_with(active_seconds):
    """抽出 sessions_endpoint() 的源码，用桩 config() 驱动，返回它生成的端点。"""
    tree = _ast.parse(open(_THROTTLE_SRC, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, _ast.FunctionDef) and n.name == "sessions_endpoint")
    ns = {"config": lambda: {"session_active_seconds": active_seconds}}
    exec(compile(_ast.Module(body=[fn], type_ignores=[]), "<throttle>", "exec"), ns)
    return ns["sessions_endpoint"]()


check("6a sessions_endpoint() 必须带 ActiveWithinSeconds 过滤（否则每小时多拉几百 MB）",
      _sessions_endpoint_with(300) == "/emby/Sessions?ActiveWithinSeconds=300",
      _sessions_endpoint_with(300))
check("6b 窗口值可配（120 → 120，0 → 退回裸端点）",
      _sessions_endpoint_with(120) == "/emby/Sessions?ActiveWithinSeconds=120"
      and _sessions_endpoint_with(0) == "/emby/Sessions",
      f"{_sessions_endpoint_with(120)} / {_sessions_endpoint_with(0)}")
check("6c 真实调用记录里也带上了这个参数（生产代码确实用了它）",
      bool(SESSIONS_LIST_RAW) and all("ActiveWithinSeconds=" in ep for ep in SESSIONS_LIST_RAW),
      str(SESSIONS_LIST_RAW))


print()
print("=" * 70)

print()
print("=" * 70)
print(f"结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 70)
sys.exit(0 if (FAIL == 0 and PASS > 0) else 1)
