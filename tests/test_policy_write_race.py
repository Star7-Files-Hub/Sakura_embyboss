#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略写入竞态测试（针对 task-44 对抗性验证报告的 H1 / H2）

## 为什么需要这套

`POST /emby/Users/{id}/Policy` 是**整份替换**语义。所有写入都必须
「读最新策略 → 只改自己负责的字段 → 写回」。这个读-改-写窗口一旦被另一个写入者
插进来，**后写的一方就把前者的改动抹掉了**。

task-44 的验证者用**真实代码 + 真实 Emby 4.10** 复现出两个后果：

  · **H1 管理员的封禁被悄悄解开**：普通用户点一次「🎬 显示/隐藏媒体库」，若管理员
    恰好在 `GET /Users/{id}` 与 `POST /Policy` 之间封禁该用户，这次写入就用 stale
    快照把 `IsDisabled` 覆盖回 `false`。
  · **H2 永久锁死（更严重）**：反向交错留下「`IsDisabled=true` + `kick_until=NULL`」。
    `_restore_kick` 的接管护栏会跳过、`sql_get_pending_kicks()` 也查不到（它只查
    `kick_until IS NOT NULL`），于是**没有任何机制会解封**，而且**群里不告警**。

## 本套件怎么测

不模拟那条真实的长窗口（那要靠网络时序），而是**构造出等价的交错**：

  · 第一个写入者的 `GET` 在返回前反复让出控制权（= 「窗口还开着」）；
  · 第二个写入者的 `GET` 立即返回，于是它能**整个跑完**并落在第一个的窗口里。

这正是 H1/H2 的本质 —— 窗口存在 + 有并发写入者。

然后断言**修复后的不变量**：两个写入者必须**串行化**，最终状态必须是「后发起的那次
写入想要的状态」，而不是被 stale 快照覆盖回旧值。

最后用**变异**证明本套件有判别力：把 `_POLICY_WRITE_LOCK` 换成空操作上下文管理器后，
§1 / §2 必须**变红**（复现 H1 / H2）。变异后仍然绿 = 本套件测不出任何东西。

运行：python3 tests/test_policy_write_race.py
"""
import ast
import asyncio
import copy
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    """抽出真实方法定义（方法在类体里，所以 walk 整棵树）。"""
    hits = [n for n in ast.walk(ast.parse(SRC_TEXT))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(hits) != 1:
        raise AssertionError(f"源码里 {name} 命中 {len(hits)} 次（期望 1 次），无法确定抽哪个")
    return textwrap.dedent(ast.get_source_segment(SRC_TEXT, hits[0]))


METHODS = ("update_user_enabled_folder", "emby_change_policy", "set_user_disabled", "get_user")
EXTRACTED = ("create_policy",) + METHODS
FN_SRC = "\n\n\n".join(extract(n) for n in EXTRACTED)


# ───────────────────────── 替身环境 ─────────────────────────

class Res:
    def __init__(self, ok, data=None, error=None):
        self.success, self.data, self.error = ok, data, error


LOGS = []
DB_WRITES = []


class _EmbyCol:
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
    "List": List,
    "Dict": Dict,
    "Any": Any,
    "extra_emby_libs": ["成人", "里番"],
    "Emby": _Emby,
    "sql_update_emby": _sql_update_emby,
    # 真实锁的替身：函数在**调用时**按名字从 ns 取，所以可以逐个场景替换（变异用）
    "_POLICY_WRITE_LOCK": asyncio.Lock(),
}
exec(compile(FN_SRC, str(SRC), "exec"), ns)   # noqa: S102 - 被测代码就是本仓库源码


class _NullLock:
    """空操作锁：用于证明「有锁」本身是判别力来源（变异用）。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeEmby:
    """只替换网络边界 `_request`；被测方法都是**真实源码**。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def _request(self, method, endpoint, **kw):
        self.calls.append((method, endpoint, kw.get("json")))
        return await self.handler(method, endpoint, kw)


for _m in METHODS:
    setattr(FakeEmby, _m, ns[_m])


print("=" * 78)
print("0. 环境自检：抽取的函数引用的全局都必须有替身（缺了要报 FAIL，不是崩）")
print("=" * 78)
_builtins = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
_loaded = {n.id for n in ast.walk(ast.parse(FN_SRC))
           if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
_stored = {n.id for n in ast.walk(ast.parse(FN_SRC))
           if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
_args = {a.arg for a in ast.walk(ast.parse(FN_SRC)) if isinstance(a, ast.arg)}
# `except Exception as e:` 里的 e 在 AST 里是 ExceptHandler.name（一个字符串），
# **不是** Name(Store) 节点，不补进来的话会被误判成"缺失的全局替身"。
_exc = {h.name for h in ast.walk(ast.parse(FN_SRC))
        if isinstance(h, ast.ExceptHandler) and h.name}
_missing = sorted(_loaded - _stored - _args - _exc - set(ns) - set(_builtins))
check("0.1 抽取代码引用的全局都有替身", not _missing, f"缺失: {_missing}")
check("0.2 真实源码里确实用了 _POLICY_WRITE_LOCK",
      "_POLICY_WRITE_LOCK" in SRC_TEXT, "")


# ───────────────────────── 公共替身 ─────────────────────────

def make_store():
    return {
        "IsDisabled": False,
        "IsAdministrator": False,
        "SimultaneousStreamLimit": 5,
        "EnableRemoteControlOfOtherUsers": True,
        "EnabledFolders": [],
        "EnableAllFolders": True,
        "BlockedMediaFolders": [],
    }


async def _false():
    """`before_write` 回调的失败形态（落库失败）。"""
    return False


class World:
    """
    一个可控的策略存储 + `_request` 替身。

    关键能力：**只有第一个写入者的 GET 会让出控制权**，于是第二个写入者能整个跑完
    并落在第一个的窗口里 —— 这就是我们要复现的交错。
    """

    def __init__(self, yield_times=20):
        self.store = make_store()
        self.yield_times = yield_times
        self.first_get_done = False
        self.svc = FakeEmby(self._handler)

    async def _handler(self, method, endpoint, kw):
        if method == "GET":
            snapshot = copy.deepcopy(self.store)
            if not self.first_get_done:
                self.first_get_done = True
                for _ in range(self.yield_times):
                    await asyncio.sleep(0)      # 「窗口还开着」
            return Res(True, {"Id": "u1", "Name": "toe", "Policy": snapshot})
        body = copy.deepcopy(kw.get("json") or {})
        self.store.clear()
        self.store.update(body)
        return Res(True, {})

    @property
    def seq(self):
        return [(m, e.rsplit("/", 1)[-1]) for (m, e, _b) in self.svc.calls]

    @property
    def posts(self):
        return [b for (m, _e, b) in self.svc.calls if m == "POST"]


async def run_pair(world, second, use_real_lock):
    """先起媒体库写入，让它的 GET 进入让出状态，再起第二个写入者，一起等完。"""
    old = ns["_POLICY_WRITE_LOCK"]
    ns["_POLICY_WRITE_LOCK"] = old if use_real_lock else _NullLock()
    try:
        t1 = asyncio.create_task(
            world.svc.update_user_enabled_folder("u1", enabled_folder_ids=["f1"])
        )
        await asyncio.sleep(0)      # 让 t1 先进入 GET（它会开始反复让出）
        await asyncio.sleep(0)
        t2 = asyncio.create_task(second(world))
        res = await asyncio.gather(t1, t2, return_exceptions=True)
    finally:
        ns["_POLICY_WRITE_LOCK"] = old
    return res


async def scenario_a(use_real_lock):
    """H1：媒体库写入窗口内，管理员封禁该用户。"""
    w = World()

    async def ban(world):
        # 真实的封禁入口，它自己也走锁
        return await world.svc.emby_change_policy("u1", admin=False, disable=True)

    res = await run_pair(w, ban, use_real_lock)
    return {"final_disabled": bool(w.store.get("IsDisabled")), "seq": w.seq,
            "posts": w.posts, "res": res}


async def scenario_b(use_real_lock):
    """H2：媒体库写入窗口内，踢流的**还原**跑完（走真实的 set_user_disabled）。"""
    w = World()
    w.store["IsDisabled"] = True          # 踢流态
    w.first_get_done = False

    async def restore(world):
        # 真实还原路径：set_user_disabled(False)（它自己也走锁）
        return await world.svc.set_user_disabled("u1", False)

    res = await run_pair(w, restore, use_real_lock)
    return {"final_disabled": bool(w.store.get("IsDisabled")), "seq": w.seq,
            "posts": w.posts, "res": res}


async def main():
    print()
    print("=" * 78)
    print("1. H1：媒体库写入 与 管理员封禁 竞态")
    print("=" * 78)
    a = await scenario_a(use_real_lock=True)
    print(f"  修复后：最终 IsDisabled={a['final_disabled']}  序列={a['seq']}")
    check("1.1 串行化后后发起的封禁生效（最终 IsDisabled=True）",
          a["final_disabled"] is True,
          f"实际={a['final_disabled']} —— 封禁被 stale 快照盖掉了")
    check("1.2 两个写入者都正常返回（锁没把谁饿死）",
          all(not isinstance(x, BaseException) for x in a["res"]), f"结果={a['res']}")
    check("1.3 提交顺序是串行的（第一个写入者的 GET+POST 相邻）",
          a["seq"][:2] == [("GET", "u1"), ("POST", "Policy")], f"序列={a['seq']}")

    print()
    print("=" * 78)
    print("2. H2：媒体库写入 与 踢流还原 竞态")
    print("=" * 78)
    b = await scenario_b(use_real_lock=True)
    print(f"  修复后：最终 IsDisabled={b['final_disabled']}  序列={b['seq']}")
    check("2.1 还原在后 → 不得被 stale 快照盖回禁用（最终 IsDisabled=False）",
          b["final_disabled"] is False,
          f"实际={b['final_disabled']} —— 这就是 H2 的永久锁死（无告警、无自动恢复）")

    print()
    print("=" * 78)
    print("3. 变异：_POLICY_WRITE_LOCK 换成空操作 → H1/H2 必须复现")
    print("=" * 78)
    ma = await scenario_a(use_real_lock=False)
    print(f"  空锁 H1：最终 IsDisabled={ma['final_disabled']}  序列={ma['seq']}")
    check("3.1 空锁时 H1 复现（最终 IsDisabled=False = 管理员封禁被解开）",
          ma["final_disabled"] is False,
          f"实际={ma['final_disabled']} —— 变异没复现，说明本套件测不出该竞态")
    check("3.2 空锁时第二个写入者的 GET 确实插进了第一个的窗口",
          len(ma["seq"]) >= 4 and ma["seq"][1][0] == "GET",
          f"序列={ma['seq']}")

    mb = await scenario_b(use_real_lock=False)
    print(f"  空锁 H2：最终 IsDisabled={mb['final_disabled']}  序列={mb['seq']}")
    check("3.3 空锁时 H2 复现（最终 IsDisabled=True = 用户被永久锁死）",
          mb["final_disabled"] is True,
          f"实际={mb['final_disabled']} —— 变异没复现")

    print()
    print("=" * 78)
    print("4. 拒绝写入：读不到策略时不得用空对象整份覆盖")
    print("=" * 78)

    async def fail_handler(method, endpoint, kw):
        return Res(False, error="boom") if method == "GET" else Res(True, {})

    svc = FakeEmby(fail_handler)
    ok = await svc.update_user_enabled_folder("u1", enabled_folder_ids=["f1"])
    check("4.1 GET 失败 → 返回 False", ok is False, f"实际={ok!r}")
    check("4.2 GET 失败 → 零 POST（不能用空策略覆盖）",
          not [c for c in svc.calls if c[0] == "POST"], f"调用={svc.calls}")

    async def empty_handler(method, endpoint, kw):
        return Res(True, {"Id": "u1", "Policy": {}}) if method == "GET" else Res(True, {})

    svc2 = FakeEmby(empty_handler)
    ok2 = await svc2.update_user_enabled_folder("u1", enabled_folder_ids=["f1"])
    check("4.3 策略为空 → 返回 False", ok2 is False, f"实际={ok2!r}")
    check("4.4 策略为空 → 零 POST（否则会把该用户所有策略重置成默认值）",
          not [c for c in svc2.calls if c[0] == "POST"], f"调用={svc2.calls}")

    print()
    print("=" * 78)
    print("5. 传给 current_policy 会被忽略并告警（旧快照捷径已废除）")
    print("=" * 78)
    LOGS.clear()
    w3 = World(yield_times=0)
    stale = make_store()
    stale["IsDisabled"] = True          # 调用方手里的旧快照（说他被禁用）
    w3.store["IsDisabled"] = False      # 真实当前状态
    ok3 = await w3.svc.update_user_enabled_folder("u1", enabled_folder_ids=["f1"],
                                                 current_policy=stale)
    check("5.1 仍然写入成功", ok3 is True, f"实际={ok3!r}")
    check("5.2 用的是**重新读到**的 IsDisabled=False，而不是传入的旧快照 True",
          w3.store.get("IsDisabled") is False,
          f"实际={w3.store.get('IsDisabled')} —— 旧快照被采纳了，这正是 H1 的成因")
    check("5.3 收到 current_policy 会记一条 warning",
          any(l == "warning" and "current_policy" in m for (l, m) in LOGS),
          f"日志={LOGS}")

    print()
    print("=" * 78)
    print("6. P1 回归：落库（before_write）必须与写策略同处一把锁内")
    print("=" * 78)
    print("   场景：A 走「临时封禁踢流」的落库+禁用；B 同时是别的策略写入者。")
    print("   若落库在锁外做，B 能插进 A 的落库与写策略之间 —— 终态会是")
    print("   「IsDisabled=true + kick_until=NULL」，即零告警的永久锁死。")

    async def scenario_p1(use_real_lock):
        store = make_store()
        order = []
        first_get = {"done": False}

        async def handler(method, endpoint, kw):
            if method == "GET":
                snap = copy.deepcopy(store)
                if not first_get["done"]:
                    first_get["done"] = True
                    for _ in range(20):
                        await asyncio.sleep(0)
                return Res(True, {"Id": "u1", "Policy": snap})
            body = copy.deepcopy(kw.get("json") or {})
            store.clear()
            store.update(body)
            return Res(True, {})

        svc = FakeEmby(handler)
        holder = {}

        async def persist_marker():
            """
            等价于 kick_user_streams 里写 kick_until 的那个回调。

            关键：**在这个回调内部**启动 B。这样 B 必然是在 A 的临界区正中间发起的，
            于是「B 能不能插进 A 的落库与写策略之间」就被精确测出来 —— 而不是靠
            两个任务的启动时序碰运气。
            """
            order.append("persist")
            holder["b"] = asyncio.create_task(writer_b())
            for _ in range(10):
                await asyncio.sleep(0)
            return True

        async def writer_a():
            ok = await svc.set_user_disabled("u1", True, before_write=persist_marker)
            order.append("A_POST done")
            return ok

        async def writer_b():
            order.append("B_enter")
            r = await svc.emby_change_policy("u1", admin=False, disable=False)
            order.append("B_POST done")
            return r

        old = ns["_POLICY_WRITE_LOCK"]
        ns["_POLICY_WRITE_LOCK"] = old if use_real_lock else _NullLock()
        try:
            ta = asyncio.create_task(writer_a())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # B 由 persist_marker 内部创建；这里等 A 先跑到落库
            for _ in range(50):
                if "persist" in order:
                    break
                await asyncio.sleep(0)
            if holder.get("b"):
                await asyncio.gather(ta, holder["b"], return_exceptions=True)
            else:
                await ta
        finally:
            ns["_POLICY_WRITE_LOCK"] = old
        return {"order": order, "seq": [(m, e.rsplit("/", 1)[-1]) for (m, e, _b) in svc.calls]}

    p1 = await scenario_p1(use_real_lock=True)
    print(f"  修复后顺序：{p1['order']}")
    print(f"  调用序列： {p1['seq']}")
    a_idx = p1["order"].index("A_POST done")
    b_idx = p1["order"].index("B_POST done")
    check("6.1 B 的写入排在 A 的写入之后（B 插不进 A 的「落库 → 写策略」）",
          b_idx > a_idx,
          f"顺序={p1['order']} —— B 在 A 写策略之前就完成了，说明落库没和写策略原子")

    p1m = await scenario_p1(use_real_lock=False)
    print(f"  空锁顺序：  {p1m['order']}")
    print(f"  空锁序列：  {p1m['seq']}")
    a_idx_m = p1m["order"].index("A_POST done")
    b_idx_m = p1m["order"].index("B_POST done")
    check("6.2 空锁时 B 确实插进了 A 的临界区（证明 6.1 有判别力）",
          b_idx_m < a_idx_m,
          f"顺序={p1m['order']} —— 变异没复现，6.1 测不出东西")

    print()
    print("=" * 78)
    print("7. P1 回归：before_write 失败时一次策略写入都不能发生")
    print("=" * 78)
    w4 = World(yield_times=0)
    ok4 = await w4.svc.set_user_disabled("u1", True, before_write=lambda: _false())
    check("7.1 回调返回 False → 本函数返回 False", ok4 is False, f"实际={ok4!r}")
    check("7.2 回调返回 False → 零 POST（不能在没落库的情况下把人禁掉）",
          not [c for c in w4.svc.calls if c[0] == "POST"], f"调用={w4.svc.calls}")

    w5 = World(yield_times=0)
    seen = []

    async def cb_ok():
        seen.append("cb")
        return True

    ok5 = await w5.svc.set_user_disabled("u1", True, before_write=cb_ok)
    posts = [c for c in w5.svc.calls if c[0] == "POST"]
    check("7.3 回调成功 → 写入成功", ok5 is True, f"实际={ok5!r}")
    check("7.4 回调确实被调用了，且写入发生了", seen == ["cb"] and len(posts) == 1,
          f"seen={seen} posts={len(posts)}")

    w6 = World(yield_times=0)
    ok6 = await w6.svc.set_user_disabled("u2", True, before_write=None)
    check("7.5 不传回调时行为不变（仍然正常写入）",
          ok6 is True and len([c for c in w6.svc.calls if c[0] == "POST"]) == 1,
          f"ok={ok6!r} calls={w6.svc.calls}")

    w7 = World(yield_times=0)
    w7.store["IsDisabled"] = True          # 已经是目标状态
    ran = []

    async def cb_never():
        ran.append("should-not-run")
        return True

    ok7 = await w7.svc.set_user_disabled("u1", True, before_write=cb_never)
    check("7.6 已经是目标状态时提前返回，**不得**执行落库回调",
          ok7 is True and ran == [],
          f"ok={ok7!r} ran={ran} —— 落标记却没有改 IsDisabled 会让还原任务解开别人的封禁")

    print()
    print("=" * 78)
    print(f"结果：PASS={PASS}  FAIL={FAIL}")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
