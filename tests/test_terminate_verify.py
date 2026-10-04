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
API_CALLS = []        # (method, endpoint)
SESSIONS_QUEUE = []   # 每次 GET /emby/Sessions 依次返回的会话列表

def make_emby():
    class E:
        async def _request(self, method, endpoint, **kw):
            API_CALLS.append((method, endpoint))
            if method == "GET" and endpoint == "/emby/Sessions":
                return SESSIONS_QUEUE.pop(0) if SESSIONS_QUEUE else Res(True, [])
            if "Playing/Stop" in endpoint:
                return Res(True, b"")
            if endpoint.endswith("/Message"):
                return Res(True, b"")
            return Res(True, {})
        async def emby_change_policy(self, emby_id, admin=False, disable=False):
            return True
    return E()

emby_stub = make_emby()

bot_mod = types.ModuleType("bot")
bot_mod.bot = object()
bot_mod.group = [-1004344766463]
bot_mod.config = Cfg()
class L:
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass
    def debug(self, m): pass
bot_mod.LOGGER = L()

emby_pkg = types.ModuleType("bot.func_helper.emby")
emby_pkg.emby = emby_stub
async def _not_admin(*a, **k): return False
emby_pkg.is_emby_admin = _not_admin

msg_utils = types.ModuleType("bot.func_helper.msg_utils")
async def _sendMessage(*a, **k): pass
msg_utils.sendMessage = _sendMessage

utils = types.ModuleType("bot.func_helper.utils")
utils.judge_admins = lambda uid: False

sql_emby = types.ModuleType("bot.sql_helper.sql_emby")
class EmbyCol:
    def __eq__(self, o): return True
class EmbyRow:
    tg = EmbyCol()
sql_emby.Emby = EmbyRow
sql_emby.sql_get_emby = lambda **k: None
sql_emby.sql_update_emby = lambda *a, **k: None

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

print()
print("=" * 70)
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
check("全断开 → 文案含『已确认全部断开』", "已确认全部断开" in GROUP_MSGS[-1], GROUP_MSGS[-1][:80])
check("全断开 → 数字为 2/2", "2/2" in GROUP_MSGS[-1], GROUP_MSGS[-1][:80])

GROUP_MSGS.clear(); SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(True, [sess("s1")]))       # 还剩一个
asyncio.run(cpm._verify_stop_followup(["s1", "s2"], "toe", wait=0))
check("部分未断 → 含『仍在播放』", "仍在播放" in GROUP_MSGS[-1], GROUP_MSGS[-1][:120])
check("部分未断 → 已断开=1", "**1**" in GROUP_MSGS[-1], GROUP_MSGS[-1][:120])

GROUP_MSGS.clear(); SESSIONS_QUEUE.clear()
SESSIONS_QUEUE.append(Res(False, error="boom"))      # 取不到
asyncio.run(cpm._verify_stop_followup(["s1"], "toe", wait=0))
check("取不到 → 文案含『未能确认』", "未能确认" in GROUP_MSGS[-1], GROUP_MSGS[-1][:120])

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
cpm.sql_update_emby = lambda *a, **k: None
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
check("首报提示复验稍后补发", "复验结果将在" in first)
second = GROUP_MSGS[1] if len(GROUP_MSGS) > 1 else ""
check("复验报实测结果（仍在播放）", "仍在播放" in second, second[:100])
check("复验数字正确（已断开 0）", "**0**" in second, second[:120])
check("私信不谎称已终止", "所有播放流已被强制终止" not in (DM_MSGS[0] if DM_MSGS else ""))
check("私信说明客户端有延迟", "20~30 秒" in (DM_MSGS[0] if DM_MSGS else ""))

# 场景二：复验时确实都断了
async def main2():
    GROUP_MSGS.clear(); DM_MSGS.clear(); SESSIONS_QUEUE.clear()
    SESSIONS_QUEUE.append(Res(True, [sess("s1"), sess("s2")]))
    SESSIONS_QUEUE.append(Res(True, []))     # 复验时已全部断开
    await cpm.check_concurrent_play_limit()
    await asyncio.sleep(0.2)
asyncio.run(main2())
check("复验全断开 → 文案『已确认全部断开 2/2』",
      len(GROUP_MSGS) == 2 and "已确认全部断开" in GROUP_MSGS[1] and "2/2" in GROUP_MSGS[1],
      GROUP_MSGS[1][:120] if len(GROUP_MSGS) > 1 else "无第二条")

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

print()
print("=" * 70)
print(f"结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 70)
sys.exit(0 if (FAIL == 0 and PASS > 0) else 1)
