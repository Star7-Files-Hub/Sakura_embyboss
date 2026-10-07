#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emby.py 策略函数本体测试（task-40 C 部分）

上一轮报告里我指出 `bot/func_helper/emby.py` 的 `get_user` / `is_user_disabled` /
`set_user_disabled` 是**零覆盖**。这里补上，用 AST 抽出**真实函数体**，只把网络边界
`self._request` 换成替身（绝不发真请求）。

本套件最重要的安全属性（C.1）：`set_user_disabled()` 必须**只改 IsDisabled 一个
字段**，把读到的 policy 里其它字段原样带回。若有人图省事改成调用
`emby_change_policy()`（它用 `create_policy()` **整份覆盖**），管理员的个性化设置
（同时播放上限、远程控制、码率限制、可见媒体库…）会被静默重置 —— 这是本功能
最大的隐性风险。所以这里既断言"最小化写入"，也断言"整份覆盖会丢字段"（对照组），
证明本套件确实能区分这两条路径。

运行：python3 tests/test_emby_policy.py
"""
import ast
import asyncio
import copy
import sys
import textwrap
from pathlib import Path
from typing import Optional

PASS, FAIL = 0, 0
REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "bot/func_helper/emby.py"
SRC_TEXT = SRC.read_text(encoding="utf-8")


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def extract(name):
    """抽出真实函数/方法定义（方法在类体里，所以要 walk 整棵树）。"""
    hits = [n for n in ast.walk(ast.parse(SRC_TEXT))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(hits) != 1:
        raise AssertionError(f"源码里 {name} 命中 {len(hits)} 次（期望 1 次），无法确定抽哪个")
    return textwrap.dedent(ast.get_source_segment(SRC_TEXT, hits[0]))


METHODS = ("get_user", "is_user_disabled", "set_user_disabled", "emby_change_policy")
EXTRACTED = ("create_policy",) + METHODS
FN_SRC = "\n\n\n".join(extract(n) for n in EXTRACTED)


# ───────────────────────── 替身环境 ─────────────────────────

class Res:
    """模拟 emby.EmbyApiResult。"""

    def __init__(self, ok, data=None, error=None):
        self.success, self.data, self.error = ok, data, error


LOGS = []
DB_WRITES = []          # emby_change_policy 里清 kick_until 的写入


class _EmbyCol:
    """模拟 Emby.embyid，使 `Emby.embyid == emby_id` 产出可识别的 where 条件。"""

    def __eq__(self, other):
        return ("embyid", other)


class _Emby:
    embyid = _EmbyCol()


def _sql_update_emby(where, **kw):
    DB_WRITES.append((where, dict(kw)))
    return True


class _Logger:
    def _rec(self, lvl, m):
        LOGS.append((lvl, str(m)))

    def info(self, m, *a, **k): self._rec("info", m)
    def warning(self, m, *a, **k): self._rec("warning", m)
    def error(self, m, *a, **k): self._rec("error", m)
    def debug(self, m, *a, **k): self._rec("debug", m)


ns = {
    "LOGGER": _Logger(),
    "Optional": Optional,
    # create_policy 里 `block = ['播放列表'] + extra_emby_libs`，真实值来自 bot 配置
    "extra_emby_libs": ["成人", "里番"],
    "Emby": _Emby,
    "sql_update_emby": _sql_update_emby,
}
exec(compile(FN_SRC, str(SRC), "exec"), ns)   # noqa: S102 - 被测代码就是本仓库源码


class FakeEmby:
    """只替换网络边界 `_request`；四个被测方法都是**真实源码**。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []          # [(method, endpoint, json_body)]

    async def _request(self, method, endpoint, **kw):
        self.calls.append((method, endpoint, kw.get("json")))
        return await self.handler(method, endpoint, kw)


for _m in METHODS:
    setattr(FakeEmby, _m, ns[_m])


def fake(handler):
    LOGS.clear()
    DB_WRITES.clear()
    return FakeEmby(handler)


def get_handler(user_result, post_result=None, get_user_result=None):
    """GET /emby/Users/{id} → user_result；POST .../Policy → post_result。"""
    async def handler(method, endpoint, kw):
        if method == "GET":
            if endpoint.endswith("/Policy"):
                return Res(False, error="unexpected")
            return get_user_result if get_user_result is not None else user_result
        return post_result if post_result is not None else Res(True, {})
    return handler


# 一份"有管理员个性化设置"的策略：全是 create_policy() 默认值之外的取值
CUSTOM_POLICY = {
    "IsDisabled": False,
    "IsAdministrator": False,
    "SimultaneousStreamLimit": 5,                     # 默认 2 → 管理员调过
    "EnableRemoteControlOfOtherUsers": True,          # 默认 False
    "RemoteClientBitrateLimit": 12345678,             # 默认 0
    "EnabledFolders": ["f1", "f2", "f3"],             # 默认 []
    "EnableAllFolders": False,
    "BlockedMediaFolders": ["成人"],
    "EnableVideoPlaybackTranscoding": True,           # 默认 False
    "EnableContentDownloading": True,                 # 默认 False
    "EnableMediaPlayback": True,
    "IsHidden": True,
    "ManagedUsers": [{"Id": "x"}],                    # 嵌套结构也要原样带回
}


def user_obj(policy):
    return {"Id": "u1", "Name": "toe", "Policy": policy}


def posts(f):
    return [(m, e, b) for (m, e, b) in f.calls if m == "POST"]


print("=" * 78)
print("0. 环境自检：抽取的函数引用的全局都必须有替身（缺了要报 FAIL，不是崩）")
print("=" * 78)
_builtins = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
_missing = sorted({n.id for n in ast.walk(ast.parse(FN_SRC))
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                  - {n.id for n in ast.walk(ast.parse(FN_SRC))
                     if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
                  - {a.arg for a in ast.walk(ast.parse(FN_SRC)) if isinstance(a, ast.arg)}
                  - {h.name for h in ast.walk(ast.parse(FN_SRC))
                     if isinstance(h, ast.ExceptHandler) and h.name}
                  - set(ns) - set(_builtins))
check("抽取的 5 个函数都已从真实源码抽出", all(callable(ns.get(n)) for n in EXTRACTED), str(EXTRACTED))
check("引用到的全局在替身命名空间里都有定义", _missing == [], f"缺少 {_missing}")

print()
print("=" * 78)
print("1. 【最重要】最小化写入：只改 IsDisabled，其它策略字段原样带回")
print("=" * 78)
snapshot = copy.deepcopy(CUSTOM_POLICY)
f = fake(get_handler(Res(True, user_obj(copy.deepcopy(CUSTOM_POLICY)))))
ok = asyncio.run(f.set_user_disabled("u1", True))
p = posts(f)
check("1a 写入成功返回 True", ok is True, str(ok))
check("1b 恰好发了一次 POST，且目标是该用户的 Policy",
      len(p) == 1 and p[0][1] == "/emby/Users/u1/Policy", str([(m, e) for (m, e, _b) in f.calls]))
check("1c 先读后写（GET 在 POST 之前）",
      [m for (m, _e, _b) in f.calls] == ["GET", "POST"], str([m for (m, _e, _b) in f.calls]))
body = p[0][2] if p else {}
check("1d body 里 IsDisabled 被置为 True", body.get("IsDisabled") is True, str(body.get("IsDisabled")))
check("1e body 的字段集合与读到的 policy **完全一致**（不多不少，没有被 create_policy 换掉）",
      set(body) == set(snapshot), f"多出={sorted(set(body) - set(snapshot))} 丢失={sorted(set(snapshot) - set(body))}")
_diff = {k: (snapshot[k], body.get(k)) for k in snapshot if k != "IsDisabled" and body.get(k) != snapshot[k]}
check("1f 除 IsDisabled 外**每个字段都逐值相等**（含列表/嵌套 dict）", _diff == {}, str(_diff))
check("1g 管理员调过的同时播放上限没被重置（默认是 2）",
      body.get("SimultaneousStreamLimit") == 5, str(body.get("SimultaneousStreamLimit")))
check("1h 远程控制权限没被重置", body.get("EnableRemoteControlOfOtherUsers") is True,
      str(body.get("EnableRemoteControlOfOtherUsers")))
check("1i 码率限制没被重置", body.get("RemoteClientBitrateLimit") == 12345678,
      str(body.get("RemoteClientBitrateLimit")))
check("1j 可见媒体库列表原样带回", body.get("EnabledFolders") == ["f1", "f2", "f3"],
      str(body.get("EnabledFolders")))
check("1k 嵌套结构（ManagedUsers）原样带回", body.get("ManagedUsers") == [{"Id": "x"}],
      str(body.get("ManagedUsers")))
check("1l 传出去的 body 里没有 create_policy 的痕迹（默认码率 0 / 默认上限 2 都没被塞进来）",
      body.get("RemoteClientBitrateLimit") != 0 and body.get("SimultaneousStreamLimit") != 2,
      f"limit={body.get('SimultaneousStreamLimit')} bitrate={body.get('RemoteClientBitrateLimit')}")

# 反向（解封）同样必须最小化：这是"临时踢流到期还原"真正会走的路径
CUSTOM_POLICY2 = copy.deepcopy(CUSTOM_POLICY)
CUSTOM_POLICY2["IsDisabled"] = True
f = fake(get_handler(Res(True, user_obj(copy.deepcopy(CUSTOM_POLICY2)))))
ok = asyncio.run(f.set_user_disabled("u1", False))
body2 = posts(f)[0][2] if posts(f) else {}
check("1n 解封方向（False）也只改 IsDisabled，其它字段逐值相等",
      ok is True and body2.get("IsDisabled") is False
      and {k: v for k, v in body2.items() if k != "IsDisabled"}
      == {k: v for k, v in CUSTOM_POLICY2.items() if k != "IsDisabled"},
      str(body2))

print()
print("=" * 78)
print("2. 对照组：emby_change_policy（整份覆盖）确实会丢字段 —— 证明本套件能区分两条路径")
print("=" * 78)
f = fake(get_handler(Res(True, user_obj(copy.deepcopy(CUSTOM_POLICY)))))
asyncio.run(f.emby_change_policy("u1", disable=True))
body3 = posts(f)[0][2] if posts(f) else {}
check("2a 它走的是 create_policy 的默认模板（同时播放上限被重置成 2）",
      body3.get("SimultaneousStreamLimit") == 2, str(body3.get("SimultaneousStreamLimit")))
check("2b 管理员的码率限制丢了（默认模板里没有这个字段 / 被抹成默认值）",
      body3.get("RemoteClientBitrateLimit") != 12345678,
      str(body3.get("RemoteClientBitrateLimit")))
check("2c 只保留了 3 个字段的白名单，其余自定义字段全部消失",
      "ManagedUsers" not in body3 and body3.get("EnableRemoteControlOfOtherUsers") is False,
      str(sorted(body3))[:200])
_changed = sorted(k for k in snapshot if body3.get(k) != snapshot[k])
check("2d 两条路径可区分：整份覆盖改掉了管理员的多项自定义设置",
      len(_changed) >= 3, f"被改掉的字段={_changed}")
# 这是 ban 路径与「临时封禁踢流」互斥的**唯一收口点**：任何一次显式策略写入都
# 作废还挂着的 kick_until，否则管理员刚封的人会被还原任务几十秒后悄悄解开。
check("2e 整份覆盖成功时会顺手清掉 kick_until（接管声明）",
      len(DB_WRITES) == 1 and DB_WRITES[0][1].get("kick_until", "缺失") is None
      and DB_WRITES[0][0] == ("embyid", "u1"),
      str(DB_WRITES))
f2 = fake(get_handler(Res(True, user_obj(copy.deepcopy(CUSTOM_POLICY))),
                      post_result=Res(False, error="HTTP 500")))
asyncio.run(f2.emby_change_policy("u1", disable=True))
check("2f 整份覆盖**失败**时不清 kick_until（没写成功就不算接管）",
      DB_WRITES == [], str(DB_WRITES))
f3 = fake(get_handler(Res(True, user_obj(copy.deepcopy(CUSTOM_POLICY)))))
asyncio.run(f3.set_user_disabled("u1", True))
check("2g set_user_disabled 走的是最小化写入，**不**清自己的 kick_until",
      DB_WRITES == [], str(DB_WRITES))

print()
print("=" * 78)
print("3. 策略为空/缺失/用户读不到 → 拒绝写入，一次 POST 都不发")
print("=" * 78)
for label, ures in (("Policy 为空 dict", Res(True, user_obj({}))),
                    ("Policy 为 None", Res(True, user_obj(None))),
                    ("用户读不到（GET 失败）", Res(False, error="HTTP 404")),
                    ("返回不是 dict", Res(True, ["not", "a", "dict"]))):
    f = fake(get_handler(ures))
    ok = asyncio.run(f.set_user_disabled("u1", True))
    check(f"3[{label}] 返回 False 且一次 POST 都不发",
          ok is False and posts(f) == [], f"ok={ok} posts={posts(f)}")
    check(f"3[{label}] 记了 error 日志（便于运维发现）",
          any(lvl == "error" for lvl, _ in LOGS), str(LOGS))

print()
print("=" * 78)
print("4. 状态已一致时不重复写（省一次写库/写 Emby，也避免无谓的策略 POST）")
print("=" * 78)
for label, cur, target in (("已是 True，目标 True", True, True), ("已是 False，目标 False", False, False)):
    pol = copy.deepcopy(CUSTOM_POLICY)
    pol["IsDisabled"] = cur
    f = fake(get_handler(Res(True, user_obj(pol))))
    ok = asyncio.run(f.set_user_disabled("u1", target))
    check(f"4[{label}] 返回 True 且不发 POST", ok is True and posts(f) == [],
          f"ok={ok} posts={posts(f)}")

print()
print("=" * 78)
print("5. 写入失败 → 返回 False（调用方靠它决定是否保留 kick_until 重试）")
print("=" * 78)
f = fake(get_handler(Res(True, user_obj(copy.deepcopy(CUSTOM_POLICY))), post_result=Res(False, error="HTTP 500")))
ok = asyncio.run(f.set_user_disabled("u1", True))
check("5a 返回 False", ok is False, str(ok))
check("5b 记了 error 日志", any(lvl == "error" for lvl, _ in LOGS), str(LOGS))
check("5c POST 确实发出去了（失败的是写入本身，不是被前置护栏挡掉）",
      len(posts(f)) == 1, str(posts(f)))

async def _boom(method, endpoint, kw):
    raise RuntimeError("network down")

f = fake(_boom)
ok = asyncio.run(f.set_user_disabled("u1", True))
check("5d 抛异常也返回 False（不把异常抛给调用方）", ok is False, str(ok))

print()
print("=" * 78)
print("6. is_user_disabled：读不到必须返回 None（不得当成 False）")
print("=" * 78)
for label, ures in (("GET 失败", Res(False, error="HTTP 503")),
                    ("返回不是 dict", Res(True, "nope"))):
    f = fake(get_handler(ures))
    r = asyncio.run(f.is_user_disabled("u1"))
    check(f"6[{label}] 返回 None（调用方据此走 state_unknown 护栏，绝不当成'未禁用'）",
          r is None, str(r))

f = fake(_boom)
check("6[抛异常] 返回 None", asyncio.run(f.is_user_disabled("u1")) is None)

for label, cur, want in (("IsDisabled=True", True, True), ("IsDisabled=False", False, False)):
    pol = {"IsDisabled": cur}
    f = fake(get_handler(Res(True, user_obj(pol))))
    r = asyncio.run(f.is_user_disabled("u1"))
    check(f"6[{label}] 读到 True/False 如实返回", r is want, str(r))

f = fake(get_handler(Res(True, user_obj({}))))
r = asyncio.run(f.is_user_disabled("u1"))
check("6[用户存在但 Policy 无 IsDisabled 键] 视为未禁用 False（与'读不到用户'的 None 区分开）",
      r is False, str(r))

print()
print("=" * 78)
print("7. get_user：失败返回 None，成功返回用户 dict（含 Policy）")
print("=" * 78)
f = fake(get_handler(Res(False, error="HTTP 404")))
check("7a GET 失败 → None", asyncio.run(f.get_user("u1")) is None)
f = fake(get_handler(Res(True, ["not", "dict"])))
check("7b 返回不是 dict → None", asyncio.run(f.get_user("u1")) is None)
f = fake(_boom)
check("7c 抛异常 → None（不往上抛）", asyncio.run(f.get_user("u1")) is None)
u = user_obj(copy.deepcopy(CUSTOM_POLICY))
f = fake(get_handler(Res(True, u)))
got = asyncio.run(f.get_user("u1"))
check("7d 成功 → 返回用户 dict 且带 Policy", got is u and got["Policy"]["SimultaneousStreamLimit"] == 5,
      str(got)[:120])
check("7e 请求路径正确", f.calls[0][1] == "/emby/Users/u1", str(f.calls[0][:2]))

print()
print("=" * 78)
print(f"结果：PASS={PASS}  FAIL={FAIL}")
print("=" * 78)
sys.exit(0 if (FAIL == 0 and PASS > 0) else 1)
