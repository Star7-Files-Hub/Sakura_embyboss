#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
register_throttle / 建号快路径 / 注册队列 —— 独立验证套件（红队视角）

跑法（依赖装在 /root/.pylibs-embyboss，TgCrypto/uvloop 装不上，忽略它的告警）：
    cd <repo>
    PYTHONPATH=/root/.pylibs-embyboss:. SAKURA_SKIP_AUTO_MIGRATE=1 python3 tests/test_emby_limiter.py

不联网、不连数据库、不创建任何真实 Emby 账号：所有 HTTP 层都用桩，
`register_throttle._ORIG_REQUEST` 用完必定还原（否则 uninstall() 会把桩当成"原始实现"还原）。

覆盖：
  A 闸门    batch lane 并发上限 / 最小间隔 / interactive lane 独立性
  B 熔断    连续失败开闸、冷却期不落到真实 _request、冷却结束恢复
  C 建号    emby_create 快路径请求数（≤3 写、POST Policy 恰好 1 次）+ 两条回退路径
  D 缓存    cached_virtual_folders 单飞 / TTL / 失败不抛
  E 队列    tem+reserved<=all_user 不变量 / stats 字段 / ETA / 去重 / 两种满文案
  F 压力    2013 个账号仿真：在途峰值 ≤ 配置并发、总请求数 == 3N
  G 对抗    配置映射 / uninstall 彻底性 / _STOP 粘滞 / 信号量热改泄漏 /
             config() 每请求读文件 / timeout 语义 / 熔断半开状态机 / _pace 跨 lane 串行
"""
import asyncio
import inspect
import os
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 测试用的限流参数：全部走环境变量（config() 里优先级最高），
#    不写侧车文件、不改 config.json，测完进程结束即失效 ────────────────────────
os.environ["EMBY_THROTTLE_ALERT_OWNER"] = "0"          # 别真去私聊 owner
os.environ["EMBY_THROTTLE_MIN_INTERVAL_MS"] = "0"      # 默认不限速，单用例自己调
os.environ["EMBY_THROTTLE_INTERACTIVE_MIN_INTERVAL_MS"] = "0"
os.environ["EMBY_THROTTLE_BATCH_GAP"] = "0"
os.environ["EMBY_THROTTLE_BREAKER_FAILURES"] = "3"
os.environ["EMBY_THROTTLE_BREAKER_COOLDOWN"] = "1.0"
os.environ.pop("EMBY_THROTTLE_ENABLED", None)

import aiohttp  # noqa: E402  （只用来造 ClientTimeout，不建真实连接）

import bot  # noqa: E402  加载 config.json / _open
from bot.func_helper import register_queue as rq  # noqa: E402
from bot.func_helper import register_throttle as t  # noqa: E402
from bot.func_helper.emby import Embyservice, EmbyApiResult, emby  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# 极简断言框架（与仓库其它套件一致：最后一行输出 结果：PASS=x FAIL=y）
# ══════════════════════════════════════════════════════════════════════════════
class Harness:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  [PASS] {name}")
        else:
            self.failed += 1
            self.failures.append((name, detail))
            print(f"  [FAIL] {name}")
            if detail:
                print(f"         {detail}")
        return bool(cond)

    def eq(self, name, actual, expected):
        return self.check(name, actual == expected, f"期望 {expected!r}，实际 {actual!r}")

    def ge(self, name, actual, expected):
        return self.check(name, actual >= expected, f"期望 >= {expected!r}，实际 {actual!r}")

    def le(self, name, actual, expected):
        return self.check(name, actual <= expected, f"期望 <= {expected!r}，实际 {actual!r}")


H = Harness()


def section(title):
    print()
    print("─" * 78)
    print(f"§ {title}")
    print("─" * 78)


def note(msg):
    print(f"  · {msg}")


@contextmanager
def env(**kv):
    """临时改环境变量（config() 每次调用都重读环境，所以能热改）。"""
    old = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def swap_orig(fake):
    """
    把 `register_throttle._ORIG_REQUEST` 换成统计桩。

    ⚠️ 这是**模块全局**：不还原的话 uninstall() 会把桩当成"原始实现"还原回去，
    整个进程（以及后续测试）就再也拿不到真 _request 了。所以必须 try/finally。
    """
    saved = t._ORIG_REQUEST
    t._ORIG_REQUEST = fake
    try:
        yield
    finally:
        t._ORIG_REQUEST = saved


def reset_breaker():
    t._BREAKER._fails = 0
    t._BREAKER._open_until = 0.0
    t._BREAKER._opened_times = 0
    t._BREAKER._alerted = False


def reset_all():
    reset_breaker()
    t.invalidate_virtual_folders()
    t._pace_last.clear()
    t._tick_state["count"] = 0
    t._tick_state["last"] = 0.0
    t.clear_stop()


class Counter:
    """统计桩：记录调用次数 / 在途并发峰值 / 每次调用的 timeout 参数。"""

    def __init__(self, ok=True, delay=0.0, error="HTTP 500"):
        self.ok = ok
        self.delay = delay
        self.error = error
        self.calls = 0
        self.cur = 0
        self.peak = 0
        self.endpoints = []
        self.timeouts = []

    async def __call__(self, self_obj, method, endpoint, timeout=None, **kwargs):
        self.calls += 1
        self.cur += 1
        self.peak = max(self.peak, self.cur)
        self.endpoints.append((method, endpoint))
        self.timeouts.append(timeout)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.ok:
                return SimpleNamespace(success=True, error=None, data={})
            return SimpleNamespace(success=False, error=self.error, data=None)
        finally:
            self.cur -= 1


async def batch_call(orig, endpoint="/emby/Users"):
    """在当前任务里切到 batch lane，发一次请求。"""
    t.enter_batch()
    try:
        return await t._throttled_request(None, "GET", endpoint)
    finally:
        t.exit_batch()


async def interactive_call(endpoint="/emby/Sessions"):
    """不切 lane = interactive lane（contextvar 默认 False）。"""
    return await t._throttled_request(None, "GET", endpoint)


# ══════════════════════════════════════════════════════════════════════════════
# A 闸门：并发上限 / 最小间隔 / lane 独立
# ══════════════════════════════════════════════════════════════════════════════
async def test_A_gate():
    section("A1 batch lane 并发上限（50 个任务同时投递）")
    reset_all()
    with env(EMBY_THROTTLE_BATCH_CONCURRENCY="1", EMBY_THROTTLE_MIN_INTERVAL_MS="0"):
        c = Counter(delay=0.02)
        with swap_orig(c):
            await asyncio.gather(*[batch_call(c) for _ in range(50)])
        H.eq("batch_concurrency=1：50 个并发任务的在途峰值 == 1", c.peak, 1)
        H.eq("50 次投递全部真的落到 _request", c.calls, 50)

    with env(EMBY_THROTTLE_BATCH_CONCURRENCY="4", EMBY_THROTTLE_MIN_INTERVAL_MS="0"):
        reset_all()
        c = Counter(delay=0.05)
        with swap_orig(c):
            await asyncio.gather(*[batch_call(c) for _ in range(50)])
        H.eq("batch_concurrency=4：在途峰值 == 4（闸门真的在放行，不是串行假象）", c.peak, 4)
        H.eq("50 次投递全部落到 _request", c.calls, 50)

    section("A2 最小请求间隔 register_min_interval_ms")
    with env(EMBY_THROTTLE_BATCH_CONCURRENCY="1", EMBY_THROTTLE_MIN_INTERVAL_MS="120"):
        reset_all()
        stamps = []

        async def timed(self_obj, method, endpoint, timeout=None, **kwargs):
            stamps.append(asyncio.get_running_loop().time())
            return SimpleNamespace(success=True, error=None, data={})

        with swap_orig(timed):
            for _ in range(5):
                await batch_call(timed)
        gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
        note(f"相邻 batch 请求间隔(ms): {[round(g * 1000, 1) for g in gaps]}")
        H.eq("发了 5 次请求", len(stamps), 5)
        H.ge("最小间隔 120ms：最小实测间隔 >= 115ms（含 5ms 容差）",
             round(min(gaps) * 1000, 2), 115)
        H.check("最小间隔确实生效（不是 0）", min(gaps) > 0.100,
                f"最小间隔 {min(gaps) * 1000:.1f}ms")

    section("A3 interactive lane 不被 batch lane 阻塞")
    with env(EMBY_THROTTLE_BATCH_CONCURRENCY="1",
             EMBY_THROTTLE_INTERACTIVE_CONCURRENCY="6",
             EMBY_THROTTLE_MIN_INTERVAL_MS="0",
             EMBY_THROTTLE_INTERACTIVE_MIN_INTERVAL_MS="0"):
        reset_all()
        slow = Counter(delay=0.6)
        fast = Counter(delay=0.0)
        with swap_orig(slow):
            hog = asyncio.create_task(batch_call(slow))   # 占住唯一的 batch 槽
            await asyncio.sleep(0.05)
            with swap_orig(fast):
                t0 = asyncio.get_running_loop().time()
                await interactive_call()
                dt = asyncio.get_running_loop().time() - t0
            await hog
        H.le("batch 槽被占住时，interactive 请求耗时 < 0.3s（两 lane 独立）", round(dt, 3), 0.3)
        note(f"interactive 实测耗时 {dt * 1000:.1f}ms（batch 那个请求还在睡 600ms）")

        reset_all()
        c = Counter(delay=0.05)
        with swap_orig(c):
            await asyncio.gather(*[interactive_call() for _ in range(20)])
        H.eq("interactive_concurrency=6：20 个并发巡检的在途峰值 == 6", c.peak, 6)

        reset_all()
        c2 = Counter(delay=0.01)
        with swap_orig(c2):
            await asyncio.gather(*[batch_call(c2) for _ in range(10)])
        H.eq("interactive 满载不影响 batch lane 自己的并发上限", c2.peak, 1)


# ══════════════════════════════════════════════════════════════════════════════
# B 熔断
# ══════════════════════════════════════════════════════════════════════════════
async def test_B_breaker():
    section("B1 连续失败开闸 + 冷却期不落到真实 _request + 冷却结束恢复")
    with env(EMBY_THROTTLE_BREAKER_FAILURES="3",
             EMBY_THROTTLE_BREAKER_COOLDOWN="1.0",
             EMBY_THROTTLE_BATCH_CONCURRENCY="1",
             EMBY_THROTTLE_MIN_INTERVAL_MS="0"):
        reset_all()
        fail = Counter(ok=False, error="HTTP 500")
        with swap_orig(fail):
            for _ in range(3):
                r = await batch_call(fail)
            H.eq("连续 3 次失败后 is_open() == True", t._BREAKER.is_open(), True)
            # 开闸即清零：下一次判定必须重新累积满 N 次失败。
            # （修复前这里保持 3，于是冷却一结束、只要 1 次失败就立刻再次开闸，
            #   breaker_failures 形同虚设 —— 见 §G9。）
            H.eq("开闸即清零 consecutive_failures == 0", t._BREAKER.consecutive_failures, 0)
            H.eq("熔断触发次数 == 1", t._BREAKER.opened_times, 1)
            H.eq("3 次失败请求都真的到了 _request", fail.calls, 3)

            before = fail.calls
            deadline1 = t._BREAKER._open_until
            rem1 = t._BREAKER.remaining()
            results = [await batch_call(fail) for _ in range(5)]
            after = fail.calls
            deadline2 = t._BREAKER._open_until
            rem2 = t._BREAKER.remaining()
            H.eq("冷却期内 5 次 batch 请求：真实 _request 调用次数增加 0", after - before, 0)
            H.check("冷却期内的结果全部是失败（快速失败）",
                    all(not r.success for r in results),
                    f"success 列表 = {[r.success for r in results]}")
            H.check("快速失败的 error 里带『熔断』字样",
                    all("熔断" in (r.error or "") for r in results),
                    f"error = {[r.error for r in results]}")
            # ⚠️ 这里**不能**断言 rem2 < rem1：5 次快速失败只花几十微秒，
            # 而虚拟机上的单调时钟粒度可能粗到几毫秒，两次读到的值会完全相等
            # （CI 上就是这么红的：`remaining 1.00s -> 1.00s`）。
            # 真正要守的不变量是「冷却截止时刻没有被推后」。
            H.eq("快速失败不会推后冷却截止时刻（_open_until 不变）", deadline2, deadline1)
            H.check("快速失败不会延长冷却时间（remaining 不增）", rem2 <= rem1,
                    f"remaining {rem1:.4f}s -> {rem2:.4f}s")

        await asyncio.sleep(1.2)
        H.eq("冷却 1.0s 结束后 is_open() == False（自动恢复）", t._BREAKER.is_open(), False)
        ok = Counter(ok=True)
        with swap_orig(ok):
            r = await batch_call(ok)
            H.eq("冷却结束后第一个 batch 请求重新落到真实 _request", ok.calls, 1)
            H.check("该请求成功", bool(r.success), f"success={r.success}")
        H.eq("成功一次后 consecutive_failures 归零", t._BREAKER.consecutive_failures, 0)

    section("B2 熔断期 interactive lane 走 3s 快速探测（batch 直接快速失败）")
    with env(EMBY_THROTTLE_BREAKER_FAILURES="1",
             EMBY_THROTTLE_BREAKER_COOLDOWN="5.0",
             EMBY_THROTTLE_PROBE_TIMEOUT="3.0",
             EMBY_THROTTLE_REQUEST_TIMEOUT="15.0",
             EMBY_THROTTLE_MIN_INTERVAL_MS="0"):
        reset_all()
        fail = Counter(ok=False, error="HTTP 500")
        with swap_orig(fail):
            await batch_call(fail)                     # 1 次失败即开闸
            H.eq("阈值=1：一次失败就开闸", t._BREAKER.is_open(), True)
            n0 = fail.calls
            r_batch = await batch_call(fail)
            H.eq("熔断期 batch：不落到真实 _request", fail.calls - n0, 0)
            H.check("熔断期 batch 返回失败", not r_batch.success)
            r_inter = await interactive_call("/emby/Users/u1")   # 小响应体 → 照常降级探测
            H.eq("熔断期 interactive（小响应体）：仍然落到真实 _request（探测）", fail.calls - n0, 1)
            to = fail.timeouts[-1]
            H.check("熔断期 interactive（小响应体）的 timeout == ClientTimeout(total=3.0)（probe_timeout）",
                    isinstance(to, aiohttp.ClientTimeout) and to.total == 3.0,
                    f"实际 timeout = {to!r}")
            H.check("熔断期 interactive 返回失败", not r_inter.success)
            # 大响应体必须豁免：裸 /emby/Sessions 线上实测 2.97 MB / 7.7s，
            # 套 3s 探测超时必被掐死，表现成「Emby 一熔断，巡检也跟着大面积报错」。
            n1 = fail.calls
            r_big = await interactive_call("/emby/Sessions")
            H.eq("熔断期 /emby/Sessions（大响应体）仍然落到真实 _request", fail.calls - n1, 1)
            to_big = fail.timeouts[-1]
            H.check("熔断期 /emby/Sessions 的 timeout 没被降成 probe_timeout（用 request_timeout=15s）",
                    isinstance(to_big, aiohttp.ClientTimeout) and to_big.total == 15.0,
                    f"实际 timeout = {to_big!r}")
            H.check("熔断期 /emby/Sessions 调用仍然返回（没被掐）", not r_big.success)
        reset_all()

    section("B3 熔断期的大响应体端点必须豁免 probe_timeout（修复前会被 0.5s 误杀）")
    with env(EMBY_THROTTLE_BREAKER_FAILURES="1",
             EMBY_THROTTLE_BREAKER_COOLDOWN="5.0",
             EMBY_THROTTLE_PROBE_TIMEOUT="0.5",
             EMBY_THROTTLE_REQUEST_TIMEOUT="15.0",
             EMBY_THROTTLE_MIN_INTERVAL_MS="0"):
        reset_all()
        truncated = {"v": False}

        async def slow_honoring_timeout(self_obj, method, endpoint, timeout=None, **kwargs):
            """一个本来要 1.5s 的正常请求，并且**尊重**传入的 timeout（像 aiohttp 一样）。"""
            delay = 1.5
            total = timeout.total if timeout is not None else 999.0
            if total < delay:
                truncated["v"] = True
                await asyncio.sleep(total)
            else:
                await asyncio.sleep(delay)
            return SimpleNamespace(success=True, error=None, data={})

        # 基线：没有熔断时，这个请求不被掐
        with swap_orig(slow_honoring_timeout):
            t0 = asyncio.get_running_loop().time()
            await interactive_call("/emby/Sessions")
            base = asyncio.get_running_loop().time() - t0
        H.check("无熔断时，1.5s 的正常请求能跑满（没有被降到 3s 以下）",
                not truncated["v"], "被截断了")
        note(f"基线耗时 {base:.2f}s（request_timeout=15s 不掐它）")

        truncated["v"] = False
        fail = Counter(ok=False, error="HTTP 500")
        with swap_orig(fail):
            await batch_call(fail)          # 阈值=1，一次失败就开闸
            H.eq("熔断已开", t._BREAKER.is_open(), True)

            # ① 大响应体：裸 /emby/Sessions —— 必须豁免，能跑满 1.5s
            with swap_orig(slow_honoring_timeout):
                t0 = asyncio.get_running_loop().time()
                await interactive_call("/emby/Sessions")
                dur_big = asyncio.get_running_loop().time() - t0
            note(f"熔断期裸 /emby/Sessions 耗时 {dur_big:.2f}s，被截断={truncated['v']}")
            H.check("熔断期裸 /emby/Sessions（大响应体）**不**被 probe_timeout 掐死",
                    not truncated["v"] and dur_big >= 1.4,
                    f"截断={truncated['v']} 耗时={dur_big:.2f}s")

            # ② 小响应体：仍然降级成 0.5s 快速探测（这才是 probe_timeout 的用途）
            truncated["v"] = False
            with swap_orig(slow_honoring_timeout):
                t0 = asyncio.get_running_loop().time()
                await interactive_call("/emby/Users/u1")
                dur_small = asyncio.get_running_loop().time() - t0
            note(f"熔断期小响应体 /emby/Users/u1 耗时 {dur_small:.2f}s，被截断={truncated['v']}")
            H.check("熔断期小响应体仍然被降成 0.5s 快速探测",
                    truncated["v"] and dur_small < 0.8,
                    f"截断={truncated['v']} 耗时={dur_small:.2f}s")
        reset_all()


# ══════════════════════════════════════════════════════════════════════════════
# C 建号请求数（真 emby_create + 假 HTTP 层）
# ══════════════════════════════════════════════════════════════════════════════
class FakeEmbyHTTP:
    """假 HTTP 层：按端点返回，记录每一次 (method, endpoint) 与每一次 Policy 写入体。"""

    LIBS = [
        {"Guid": "g-movie", "Name": "电影"},
        {"Guid": "g-tv", "Name": "电视"},
        {"Guid": "g-nsfw", "Name": "nsfw"},
        {"Guid": "g-playlist", "Name": "播放列表"},
    ]

    def __init__(self, fail_vf=False, user_policy=None, libs=None):
        self.calls = []
        self.policy_bodies = []
        self.libs = libs if libs is not None else self.LIBS
        self.fail_vf = fail_vf
        self.user_policy = user_policy if user_policy is not None else {
            "EnableAllFolders": True, "EnabledFolders": [], "BlockedMediaFolders": ["播放列表"],
        }
        self.user_id = "u-new-1"

    async def __call__(self, method, endpoint, timeout=None, **kwargs):
        self.calls.append((method, endpoint))
        if endpoint == "/emby/Users/New":
            return EmbyApiResult(True, {"Id": self.user_id})
        if endpoint.endswith("/Password"):
            return EmbyApiResult(True, {})
        if endpoint.endswith("/Policy"):
            self.policy_bodies.append(kwargs.get("json") or {})
            return EmbyApiResult(True, {})
        if endpoint == "/emby/Library/VirtualFolders":
            if self.fail_vf:
                return EmbyApiResult(False, error="HTTP 500")
            return EmbyApiResult(True, self.libs)
        if endpoint.startswith("/emby/Users/"):
            return EmbyApiResult(True, {"Id": self.user_id, "Policy": self.user_policy})
        return EmbyApiResult(False, error=f"未预期的端点 {endpoint}")


def new_svc(router):
    """不跑 __init__ 的 Embyservice 实例（只需要 _request / emby_del）。"""
    svc = Embyservice.__new__(Embyservice)
    svc._request = router

    async def fake_del(emby_id):
        return True

    svc.emby_del = fake_del
    return svc


def lockout_policies(router):
    """筛出会锁死用户的策略体：EnableAllFolders=False 且 EnabledFolders==[]"""
    return [b for b in router.policy_bodies
            if b.get("EnableAllFolders") is False and b.get("EnabledFolders") == []]


# 线上真实的媒体库名（Lead 实测）：emby_block=['nsfw']、extra_emby_libs=['电视']
REAL_ONLINE_LIBS = [
    {"Guid": "g-guochan", "Name": "⚔️国产·动漫"},
    {"Guid": "g-rihan", "Name": "📺日韩·剧集"},
    {"Guid": "g-other", "Name": "其他"},
    {"Guid": "g-playlist", "Name": "播放列表"},
]


async def test_C_create_request_count():
    real_cvf = t.cached_virtual_folders

    async def cvf_of(libs):
        async def _cvf(ttl=None, force=False):
            return {lib["Guid"]: lib["Name"] for lib in libs}
        return _cvf

    section("C1 分支甲（线上真实：emby_block/extra_emby_libs 的名字一个都对不上）")
    router = FakeEmbyHTTP(libs=REAL_ONLINE_LIBS)
    svc = new_svc(router)
    t.cached_virtual_folders = await cvf_of(REAL_ONLINE_LIBS)
    try:
        res = await svc.emby_create("验证用户A", 30)
    finally:
        t.cached_virtual_folders = real_cvf
    note(f"全部请求：{router.calls}")
    H.check("建号成功并返回 (user_id, password, expiry)", isinstance(res, tuple) and len(res) == 3,
            f"实际返回 {res!r}")
    H.eq("分支甲：快路径总请求数 == 3", len(router.calls), 3)
    H.le("分支甲：写请求（POST）<= 3", len([c for c in router.calls if c[0] == "POST"]), 3)
    H.eq("分支甲：POST /Users/{id}/Policy 恰好 1 次",
         len([c for c in router.calls if c == ("POST", f"/emby/Users/{router.user_id}/Policy")]), 1)
    H.eq("分支甲：没有任何 GET（媒体库走缓存）",
         len([c for c in router.calls if c[0] == "GET"]), 0)
    H.eq("分支甲：只写了 1 次 Policy", len(router.policy_bodies), 1)
    body = router.policy_bodies[0]
    H.check("分支甲：策略里**没有** EnableAllFolders 键（复刻 hide_folders_by_names 提前 return）",
            "EnableAllFolders" not in body, f"实际 {body.get('EnableAllFolders')!r}")
    H.check("分支甲：策略里**没有** EnabledFolders 键",
            "EnabledFolders" not in body, f"实际 {body.get('EnabledFolders')!r}")
    H.eq("分支甲：BlockedMediaFolders 与第 3 步 create_policy 一致",
         body.get("BlockedMediaFolders"), ["播放列表", "电视"])
    H.eq("分支甲：没有写出锁死用户的空 EnabledFolders 策略", lockout_policies(router), [])

    section("C2 分支乙（名字能对上）")
    router = FakeEmbyHTTP()
    svc = new_svc(router)
    t.cached_virtual_folders = await cvf_of(FakeEmbyHTTP.LIBS)
    try:
        res = await svc.emby_create("验证用户B", 30)
    finally:
        t.cached_virtual_folders = real_cvf
    note(f"全部请求：{router.calls}")
    H.check("建号成功", isinstance(res, tuple) and len(res) == 3, f"实际返回 {res!r}")
    H.eq("分支乙：快路径总请求数 == 3", len(router.calls), 3)
    H.eq("分支乙：POST /Users/{id}/Policy 恰好 1 次",
         len([c for c in router.calls if c == ("POST", f"/emby/Users/{router.user_id}/Policy")]), 1)
    H.eq("分支乙：只写了 1 次 Policy", len(router.policy_bodies), 1)
    body = router.policy_bodies[0]
    H.eq("分支乙：策略里 EnableAllFolders == False", body.get("EnableAllFolders"), False)
    H.eq("分支乙：策略里 EnabledFolders == 全部库 − 被隐藏的（'播放列表' 不在 emby_block/extra 里，仍可见）",
         body.get("EnabledFolders"), ["g-movie", "g-playlist"])
    H.eq("分支乙：BlockedMediaFolders 与『写两次』的最终值同集合",
         set(body.get("BlockedMediaFolders") or []), {"播放列表", "电视", "nsfw"})
    H.eq("分支乙：没有写出锁死用户的空 EnabledFolders 策略", lockout_policies(router), [])

    section("C3 回退路径一：媒体库列表为空（缓存返回 {}）→ 整条回退，不锁死用户")
    router = FakeEmbyHTTP()
    svc = new_svc(router)

    async def empty_cvf(ttl=None, force=False):
        return {}

    t.cached_virtual_folders = empty_cvf
    try:
        res = await svc.emby_create("验证用户B", 30)
    finally:
        t.cached_virtual_folders = real_cvf
    note(f"全部请求：{router.calls}")
    H.check("空库回退后仍然建号成功（不抛异常、返回 tuple）",
            isinstance(res, tuple) and len(res) == 3, f"实际返回 {res!r}")
    H.ge("走了原多请求路径（请求数 > 3）", len(router.calls), 4)
    H.eq("回退路径没有写出锁死用户的空 EnabledFolders 策略", lockout_policies(router), [])
    H.ge("回退路径至少写了 1 次 Policy", len(router.policy_bodies), 1)
    note(f"回退路径 Policy 写入体摘要：{[{k: v for k, v in b.items() if k in ('EnableAllFolders', 'EnabledFolders')} for b in router.policy_bodies]}")

    section("C4 回退路径二：cached_virtual_folders 抛异常 → 整条回退，不抛到调用方")
    router = FakeEmbyHTTP()
    svc = new_svc(router)

    async def boom_cvf(ttl=None, force=False):
        raise RuntimeError("限流模块自己坏了")

    t.cached_virtual_folders = boom_cvf
    try:
        res = await svc.emby_create("验证用户C", 30)
    finally:
        t.cached_virtual_folders = real_cvf
    H.check("缓存函数抛异常时建号仍成功（异常被吃掉并回退）",
            isinstance(res, tuple) and len(res) == 3, f"实际返回 {res!r}")
    H.eq("异常回退路径也没有写出锁死策略", lockout_policies(router), [])

    section("C5 回退路径三：GET /Library/VirtualFolders 直接 500（Emby 病了）")
    router = FakeEmbyHTTP(fail_vf=True)
    svc = new_svc(router)
    t.cached_virtual_folders = empty_cvf
    try:
        res = await svc.emby_create("验证用户D", 30)
    finally:
        t.cached_virtual_folders = real_cvf
    H.check("媒体库接口 500 时建号仍成功", isinstance(res, tuple) and len(res) == 3,
            f"实际返回 {res!r}")
    H.eq("媒体库 500 时绝不写出锁死策略（EnableAllFolders=False + EnabledFolders=[]）",
         lockout_policies(router), [])
    note(f"该场景实际写入的 Policy 次数：{len(router.policy_bodies)}；请求：{router.calls}")

    section("C6 建号失败时回滚语义（Policy 写失败 → 删孤儿账号）")
    router = FakeEmbyHTTP()
    deleted = []

    async def failing_policy(method, endpoint, timeout=None, **kwargs):
        # 注意：赋给实例属性后**不会**自动绑定 self，所以这里第一个参数就是 method
        router.calls.append((method, endpoint))
        if endpoint.endswith("/Policy"):
            router.policy_bodies.append(kwargs.get("json") or {})
            return EmbyApiResult(False, error="HTTP 500")
        return await FakeEmbyHTTP.__call__(router, method, endpoint, timeout, **kwargs)

    svc = Embyservice.__new__(Embyservice)
    svc._request = failing_policy

    async def fake_del(emby_id):
        deleted.append(emby_id)
        return True

    svc.emby_del = fake_del
    real_cvf = t.cached_virtual_folders

    async def local_cvf(ttl=None, force=False):
        return {lib["Guid"]: lib["Name"] for lib in FakeEmbyHTTP.LIBS}

    t.cached_virtual_folders = local_cvf
    try:
        res = await svc.emby_create("验证用户E", 30)
    finally:
        t.cached_virtual_folders = real_cvf
    H.eq("Policy 写失败 → 返回 False", res, False)
    H.eq("Policy 写失败 → 回滚删掉刚建的孤儿账号", deleted, ["u-new-1"])

    section("C6b 边界：POST /Users/New 返回 success 但 data 为空")
    router2 = FakeEmbyHTTP()

    async def new_no_data(method, endpoint, timeout=None, **kwargs):
        router2.calls.append((method, endpoint))
        if endpoint == "/emby/Users/New":
            return EmbyApiResult(True, None)
        return await FakeEmbyHTTP.__call__(router2, method, endpoint, timeout, **kwargs)

    svc2 = Embyservice.__new__(Embyservice)
    svc2._request = new_no_data
    deleted2 = []

    async def fake_del2(emby_id):
        deleted2.append(emby_id)
        return True

    svc2.emby_del = fake_del2
    res2 = await svc2.emby_create("验证用户G", 30)
    note(f"data=None 时返回 {res2!r}，回滚删除 {deleted2!r}")
    H.check("data=None → 返回 False（不抛到调用方）", res2 is False, f"实际 {res2!r}")
    H.check("此时不会调用 _delete_orphan_account（若 Emby 侧其实建了号就是孤儿）",
            deleted2 == [], f"{deleted2}")
    note("说明：这不是本次改动引入的 —— 基准 的 emby_create 在 "
         "`user_id = result.data.get('Id')` 处同样会抛 AttributeError 被最外层 except 吞掉后 return False。"
         "属于既有边界，建议顺手在 data 为空时也走一次回滚。")


# ══════════════════════════════════════════════════════════════════════════════
# D 媒体库缓存
# ══════════════════════════════════════════════════════════════════════════════
async def test_D_vf_cache():
    section("D1 cached_virtual_folders 单飞 + TTL")
    with env(EMBY_THROTTLE_VIRTUALFOLDERS_TTL="300"):
        reset_all()
        calls = {"n": 0}

        async def fake_request(method, endpoint, timeout=None, **kwargs):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return EmbyApiResult(True, [{"Guid": f"g{i}", "Name": f"库{i}"} for i in range(3)])

        saved = emby._request
        emby._request = fake_request
        try:
            results = await asyncio.gather(*[t.cached_virtual_folders() for _ in range(12)])
            H.eq("12 个并发调用 → 真实 HTTP 只有 1 次（单飞生效）", calls["n"], 1)
            H.check("12 个调用拿到同一份数据", all(r == results[0] for r in results))
            H.eq("数据内容正确", results[0], {"g0": "库0", "g1": "库1", "g2": "库2"})
            await t.cached_virtual_folders()
            H.eq("TTL 内再调用仍然不发请求", calls["n"], 1)
            r = await t.cached_virtual_folders(force=True)
            H.eq("force=True 强制刷新", calls["n"], 2)
            H.eq("强制刷新后数据仍正确", r, results[0])
            # 返回的是副本，改它不能污染缓存
            r["hacked"] = "x"
            again = await t.cached_virtual_folders()
            H.check("返回值是副本（改返回值不污染缓存）", "hacked" not in again,
                    f"缓存被污染：{again}")
        finally:
            emby._request = saved
        t.invalidate_virtual_folders()

    section("D2 拉取失败：不抛异常，返回空/上一次好数据")
    reset_all()

    async def fail_request(method, endpoint, timeout=None, **kwargs):
        return EmbyApiResult(False, error="HTTP 500")

    saved = emby._request
    emby._request = fail_request
    try:
        out = await t.cached_virtual_folders()
        H.eq("首次失败 → 返回 {}（不抛异常）", out, {})
        out2 = await t.cached_virtual_folders()
        H.eq("连续失败仍然是 {}（不抛异常）", out2, {})
    finally:
        emby._request = saved

    # 先成功一次，再失败 → 应该返回上一次的好数据
    reset_all()

    async def ok_request(method, endpoint, timeout=None, **kwargs):
        return EmbyApiResult(True, [{"Guid": "g9", "Name": "好库"}])

    saved = emby._request
    emby._request = ok_request
    try:
        await t.cached_virtual_folders()
    finally:
        emby._request = saved
    emby._request = fail_request
    try:
        out3 = await t.cached_virtual_folders(force=True)
        H.eq("刷新失败时退回上一次的好数据（不是空）", out3, {"g9": "好库"})
    finally:
        emby._request = saved
    t.invalidate_virtual_folders()


# ══════════════════════════════════════════════════════════════════════════════
# E 注册队列
# ══════════════════════════════════════════════════════════════════════════════
def make_job(uid, msg=None):
    return rq.RegisterJob(user_id=uid, username=f"user{uid}", pwd2="1234",
                          stats=True, days=30, status_message=msg or SimpleNamespace())


@contextmanager
def queue_env(all_user=100, tem=0, workers=2, queue_limit=300, warn=0, eta_window=10, eta_min=3):
    stub = SimpleNamespace(
        all_user=all_user, tem=tem, register_worker_count=workers,
        register_queue_limit=queue_limit, register_queue_warn_after_seconds=warn,
        register_queue_eta_window=eta_window, register_queue_eta_min_samples=eta_min,
    )
    saved = rq._open
    rq._open = stub
    try:
        yield stub
    finally:
        rq._open = saved


async def test_E_queue():
    section("E1 不变量 tem + reserved <= all_user（200 个并发 enqueue）")
    with queue_env(all_user=50, tem=0, workers=3, queue_limit=1000, warn=0):
        mgr = rq.RegisterQueueManager()
        started = asyncio.Event()

        async def fake_process(job):
            await asyncio.sleep(0.02)
            job.outcome = "ok"

        mgr._process_job = fake_process
        peak = {"v": 0}
        stop = {"v": False}

        async def monitor():
            while not stop["v"]:
                peak["v"] = max(peak["v"], int(rq._open.tem or 0) + mgr._reserved_slots)
                await asyncio.sleep(0)

        mon = asyncio.create_task(monitor())
        results = await asyncio.gather(*[mgr.enqueue(make_job(1000 + i)) for i in range(200)])
        # 等队列真正排空（worker 还在收尾时 reserved 不会归零）
        for _ in range(600):
            if mgr._reserved_slots == 0 and mgr._queue.qsize() == 0:
                break
            await asyncio.sleep(0.02)
        stop["v"] = True
        await mon

        accepted = [r for r in results if r[0]]
        rejected = [r for r in results if not r[0]]
        reasons = sorted({r[1] for r in rejected})
        H.le("200 个并发 enqueue 期间，tem + reserved 峰值 <= all_user(50)", peak["v"], 50)
        note(f"接受 {len(accepted)} 个，拒绝 {len(rejected)} 个，拒绝原因 {reasons}")
        H.check("席位满时返回 slot_full", "slot_full" in reasons, f"reasons={reasons}")
        H.check("每个成功 enqueue 返回的位置都 >= 1",
                all(isinstance(r[2], int) and r[2] >= 1 for r in accepted))
        H.eq("最终 reserved 归零（全部处理完）", mgr._reserved_slots, 0)
        H.eq("最终 busy_users 清空", len(mgr._busy_users), 0)
        for task in mgr._workers:
            task.cancel()
        await asyncio.gather(*mgr._workers, return_exceptions=True)

    section("E2 queue_full 与 slot_full 是两种不同结果（都真的会出现）")
    with queue_env(all_user=1000, tem=0, workers=1, queue_limit=3, warn=0):
        mgr = rq.RegisterQueueManager()
        release = asyncio.Event()

        async def blocking_process(job):
            await release.wait()
            job.outcome = "ok"

        mgr._process_job = blocking_process
        r1 = await mgr.enqueue(make_job(2001))          # 被唯一的 worker 立刻取走
        for _ in range(100):                            # 等 worker 真的把它取走，否则后面 qsize 会飘
            if mgr._active_jobs >= 1:
                break
            await asyncio.sleep(0.01)
        r2 = await mgr.enqueue(make_job(2002))
        r3 = await mgr.enqueue(make_job(2003))
        r4 = await mgr.enqueue(make_job(2004))          # qsize 到 3
        r5 = await mgr.enqueue(make_job(2005))          # 排队满
        H.eq("第 1 个：接受（被 worker 取走）", r1[1], "queued")
        H.eq("第 2~4 个：接受（排队）", [r2[1], r3[1], r4[1]], ["queued", "queued", "queued"])
        H.eq("第 5 个：queue_full（还有席位，只是等位的人满了）", r5[1], "queue_full")
        H.eq("queue_full 不返回位置", r5[2], None)
        H.eq("waiting_limit == min(queue_limit, all_user - tem - active_jobs) == 3",
             mgr.waiting_queue_limit(), 3)
        note(f"queue_full_message 文案首行：{rq.queue_full_message(3).splitlines()[0]}")
        note(f"slot_full_message 文案首行：{rq.slot_full_message(2).splitlines()[0]}")
        H.check("两种文案不同（不会糊成一条）",
                rq.queue_full_message(3) != rq.slot_full_message(2))
        release.set()
        await asyncio.sleep(0.1)
        for task in mgr._workers:
            task.cancel()
        await asyncio.gather(*mgr._workers, return_exceptions=True)

    section("E3 重复用户去重 + 返回位置")
    with queue_env(all_user=100, tem=0, workers=1, queue_limit=50, warn=0):
        mgr = rq.RegisterQueueManager()
        release = asyncio.Event()

        async def blocking_process(job):
            await release.wait()
            job.outcome = "ok"

        mgr._process_job = blocking_process
        a = await mgr.enqueue(make_job(3001))
        for _ in range(100):                            # 等 worker 取走第一个，位置判定才确定
            if mgr._active_jobs >= 1:
                break
            await asyncio.sleep(0.01)
        b = await mgr.enqueue(make_job(3002))
        c = await mgr.enqueue(make_job(3003))
        dup = await mgr.enqueue(make_job(3002))
        H.eq("首次接受", a[1], "queued")
        H.eq("重复提交 → duplicate", dup[1], "duplicate")
        H.check("duplicate 会回位置（不是 None）", isinstance(dup[2], int) and dup[2] > 0,
                f"实际位置 {dup[2]!r}")
        H.eq("重复提交不会多占席位（reserved == 3）", mgr._reserved_slots, 3)
        H.eq("busy_users 里该用户只算一个", len(mgr._busy_users), 3)
        H.eq("队列里也只有 3 个 job（没排进第 4 个）", mgr._queue.qsize(), 2)
        pos = mgr.user_queue_position(3002)
        H.eq("user_queue_position(3002) 与 duplicate 返回值一致", pos, dup[2])
        H.eq("正在处理中的用户位置 == 0（3001 被 worker 取走）",
             mgr.user_queue_position(3001), 0)
        H.eq("不在队列里的用户位置 == None", mgr.user_queue_position(999999), None)
        release.set()
        await asyncio.sleep(0.1)
        for task in mgr._workers:
            task.cancel()
        await asyncio.gather(*mgr._workers, return_exceptions=True)

    section("E4 stats() 字段齐全 + ETA 随样本收敛")
    with queue_env(all_user=100, tem=10, workers=2, queue_limit=300, warn=0):
        mgr = rq.RegisterQueueManager()
        st = mgr.stats()
        expected_keys = {"waiting", "active", "workers", "reserved", "avg_seconds", "samples",
                         "failures", "remaining_slots", "queue_limit", "waiting_limit", "eta_for"}
        H.eq("stats() 字段集合完整", set(st.keys()), expected_keys)
        H.eq("queue_limit 读到 register_queue_limit", st["queue_limit"], 300)
        H.eq("remaining_slots == all_user - tem - reserved == 90", st["remaining_slots"], 90)
        H.eq("没有样本时 avg_seconds 为 None", st["avg_seconds"], None)
        H.eq("没有样本时 eta_for(n) 为 None", st["eta_for"](5), None)
        H.eq("没有样本时文案是『正在估算中』", rq.format_eta(None, 0), "预计耗时正在估算中")
        H.check("样本不足（<3）时文案带『约』和区间",
                rq.format_eta(10.0, 2).startswith("预计约"), rq.format_eta(10.0, 2))
        H.check("样本充足时也带区间", rq.format_eta(10.0, 5).startswith("预计约"),
                rq.format_eta(10.0, 5))

        # ETA 收敛：样本从 4.0s 逐步被 1.0s 替换
        for _ in range(10):
            mgr._duration_samples.append((4.0, True))
        avg4, n4 = mgr._eta_inputs()
        H.eq("10 个 4.0s 样本 → 均值 4.0", round(avg4, 6), 4.0)
        H.eq("窗口内样本数 == eta_window(10)", n4, 10)
        for _ in range(10):
            mgr._duration_samples.append((1.0, True))
        avg1, n1 = mgr._eta_inputs()
        H.eq("再喂 10 个 1.0s 样本 → 窗口被替换，均值收敛到 1.0", round(avg1, 6), 1.0)
        H.eq("窗口不会无限增长", n1, 10)
        H.eq("样本数 > 0 时 eta_for(4) == 均值 * 4 / workers",
             round(mgr.eta_for(4), 6), round(1.0 * 4 / 2, 6))
        H.eq("eta_for 对 0 个人返回 0", mgr.eta_for(0), 0.0)
        mgr._duration_samples.append((2.0, False))
        H.eq("failures 计数只数失败样本", mgr.stats()["failures"], 1)
        H.check("waiting_line 里含位置与 ETA",
                "你前面还有" in mgr.waiting_line(3), mgr.waiting_line(3))
        H.check("waiting_line(0) 说队首",
                "队首" in mgr.waiting_line(0), mgr.waiting_line(0))

    section("E5 实测耗时样本被真的记账（worker 走完整路径）")
    with queue_env(all_user=100, tem=0, workers=2, queue_limit=50, warn=0):
        mgr = rq.RegisterQueueManager()

        async def fake_process(job):
            await asyncio.sleep(0.06)
            job.outcome = "ok"

        mgr._process_job = fake_process
        await mgr.enqueue(make_job(4001))
        await asyncio.sleep(0.25)
        st = mgr.stats()
        H.eq("处理完 1 个 job 后 samples == 1", st["samples"], 1)
        H.check("实测耗时 >= 60ms（真的量到了 worker 的执行时间）",
                st["avg_seconds"] is not None and st["avg_seconds"] >= 0.06,
                f"avg_seconds={st['avg_seconds']}")
        H.eq("完成后 active == 0", st["active"], 0)
        H.eq("完成后 reserved == 0", st["reserved"], 0)
        H.eq("完成后 waiting == 0", st["waiting"], 0)
        for task in mgr._workers:
            task.cancel()
        await asyncio.gather(*mgr._workers, return_exceptions=True)


# ══════════════════════════════════════════════════════════════════════════════
# F 2013 个账号压力仿真
# ══════════════════════════════════════════════════════════════════════════════
async def test_F_stress():
    section("F 2013 个账号仿真（假 Emby 每次 5ms；每个账号 3 次请求）")
    N = 2013
    CONC = 3
    with env(EMBY_THROTTLE_BATCH_CONCURRENCY=str(CONC),
             EMBY_THROTTLE_MIN_INTERVAL_MS="0",
             EMBY_THROTTLE_BATCH_GAP="0",
             EMBY_THROTTLE_SHARD_SIZE="25"):
        reset_all()
        c = Counter(delay=0.005)
        tasks_peak = {"v": 0}

        async def watch_tasks():
            while True:
                tasks_peak["v"] = max(tasks_peak["v"], len(asyncio.all_tasks()))
                await asyncio.sleep(0.02)

        async def one_account(item):
            """一个账号 = 3 次 Emby 请求（POST New / POST Password / POST Policy）。"""
            for ep in ("/emby/Users/New", "/emby/Users/x/Password", "/emby/Users/x/Policy"):
                r = await t._throttled_request(None, "POST", ep)
                if not r.success:
                    return False
            return True

        with swap_orig(c):
            watcher = asyncio.create_task(watch_tasks())
            t0 = time.perf_counter()
            report = await t.run_batch(list(range(N)), one_account, name="stress-2013")
            elapsed = time.perf_counter() - t0
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        note(f"报告：{report.as_text()}")
        note(f"实测：总请求 {c.calls} 次 / 在途峰值 {c.peak} / 耗时 {elapsed:.1f}s / "
             f"asyncio 任务数峰值 {tasks_peak['v']}")
        H.eq("run_batch 处理了全部 2013 项", report.total, N)
        H.eq("全部成功", report.ok, N)
        H.eq("没有失败项", report.failed, 0)
        H.eq("没有被中止", report.aborted, False)
        H.le("在途请求峰值 <= 配置并发 3", c.peak, CONC)
        H.eq("run_batch 内部是 `for item: await fn(item)` 的串行循环 → 在途峰值恒为 1",
             c.peak, 1)
        H.eq("总请求数 == 3 × 2013", c.calls, 3 * N)
        H.check("asyncio 同时存活任务数有上界（< 60）", tasks_peak["v"] < 60,
                f"任务数峰值 {tasks_peak['v']}")
        H.eq("无异常堆积（failures 为空）", report.failures, [])

    section("F1b 有界 worker 池（模拟 register_worker_count 个队列 worker 抢同一道闸门）")
    N2 = 2013
    POOL = 20
    with env(EMBY_THROTTLE_BATCH_CONCURRENCY="3",
             EMBY_THROTTLE_MIN_INTERVAL_MS="0",
             EMBY_THROTTLE_BATCH_GAP="0"):
        reset_all()
        c = Counter(delay=0.005)
        q = asyncio.Queue()
        for i in range(N2):
            q.put_nowait(i)
        done = {"n": 0}
        tasks_peak = {"v": 0}

        async def pool_worker():
            t.enter_batch()          # 队列 worker 走的是 _throttled_process_job，会切 batch lane
            try:
                while True:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    for ep in ("/emby/Users/New", "/emby/Users/x/Password", "/emby/Users/x/Policy"):
                        await t._throttled_request(None, "POST", ep)
                    done["n"] += 1
            finally:
                t.exit_batch()

        async def watch():
            while True:
                tasks_peak["v"] = max(tasks_peak["v"], len(asyncio.all_tasks()))
                await asyncio.sleep(0.02)

        with swap_orig(c):
            w = asyncio.create_task(watch())
            t0 = time.perf_counter()
            await asyncio.gather(*[asyncio.create_task(pool_worker()) for _ in range(POOL)])
            dt2 = time.perf_counter() - t0
            w.cancel()
            await asyncio.gather(w, return_exceptions=True)

        note(f"{POOL} 个 worker 池跑 {N2} 个账号：总请求 {c.calls} 次 / 在途峰值 {c.peak} / "
             f"耗时 {dt2:.1f}s / asyncio 任务数峰值 {tasks_peak['v']}")
        H.eq("全部 2013 个账号都建完", done["n"], N2)
        H.le("在途请求峰值 <= 配置并发 3", c.peak, CONC)
        H.eq("在途请求峰值确实顶到配置并发 3（闸门真的在限流，不是串行假象）", c.peak, CONC)
        H.eq("总请求数 == 3 × 2013", c.calls, 3 * N2)
        H.check("asyncio 同时存活任务数有上界（< 60）", tasks_peak["v"] < 60,
                f"任务数峰值 {tasks_peak['v']}")

        section("F2 分片：每 25 个插一次批间隔（可被 request_stop 打断）")
        reset_all()
        with env(EMBY_THROTTLE_BATCH_GAP="0.05", EMBY_THROTTLE_SHARD_SIZE="25"):
            t0 = time.perf_counter()
            rep = await t.run_batch(list(range(60)), lambda x: asyncio.sleep(0, result=True))
            dt = time.perf_counter() - t0
        H.ge("60 项 / 25 一批 → 至少插了 2 次批间隔（>= 0.10s）", round(dt, 3), 0.09)
        note(f"60 项耗时 {dt:.3f}s，报告：{rep.as_text()}")


# ══════════════════════════════════════════════════════════════════════════════
# G 对抗式复核
# ══════════════════════════════════════════════════════════════════════════════
async def test_G_adversarial():
    section("G1 配置映射：Open 上的 register_* 字段是不是真的被读到了")
    real_open = bot._open
    with env(EMBY_THROTTLE_ENABLED=None, EMBY_THROTTLE_MIN_INTERVAL_MS=None):
        stub = SimpleNamespace(
            register_throttle_enabled=False,      # ← 总开关（schema 里声明的名字）
            register_batch_concurrency=7,         # ← 对照组：别的键能不能读到
            register_min_interval_ms=123,
            register_session_active_seconds=456,
        )
        bot._open = stub
        try:
            cfg = t.config()
        finally:
            bot._open = real_open
    note(f"_open.register_throttle_enabled=False 时 config()['enabled'] = {cfg['enabled']}")
    H.eq("对照组：register_batch_concurrency 被正确读到", cfg["batch_concurrency"], 7)
    H.eq("对照组：register_min_interval_ms 被正确读到", cfg["min_interval_ms"], 123)
    H.eq("对照组：register_session_active_seconds 被正确读到", cfg["session_active_seconds"], 456)
    H.eq("config()['enabled'] 读到 register_throttle_enabled=False（修复前读不到，只能靠环境变量）",
         cfg["enabled"], False)
    # 兼容旧写法 register_enabled
    with env(EMBY_THROTTLE_ENABLED=None):
        stub2 = SimpleNamespace(register_enabled=False)
        bot._open = stub2
        try:
            H.eq("旧写法 register_enabled=False 也兼容", t.config()["enabled"], False)
        finally:
            bot._open = real_open

    # 环境变量这条路径是好的
    with env(EMBY_THROTTLE_ENABLED="0"):
        H.eq("环境变量 EMBY_THROTTLE_ENABLED=0 能关掉（唯一可用的关法）",
             t.config()["enabled"], False)
    reset_all()

    section("G1b sessions_endpoint() 的窗口参数 + emby.py 兜底实现是否同源")
    with env(EMBY_THROTTLE_SESSION_ACTIVE_SECONDS="300"):
        H.eq("窗口 300 → 带参数", t.sessions_endpoint(),
             "/emby/Sessions?ActiveWithinSeconds=300")
    with env(EMBY_THROTTLE_SESSION_ACTIVE_SECONDS="0"):
        H.eq("窗口 0 → 退回裸端点", t.sessions_endpoint(), "/emby/Sessions")
    with env(EMBY_THROTTLE_SESSION_ACTIVE_SECONDS="60"):
        H.eq("窗口可配", t.sessions_endpoint(), "/emby/Sessions?ActiveWithinSeconds=60")
    with env(EMBY_THROTTLE_SESSION_ACTIVE_SECONDS="300",
             EMBY_THROTTLE_BATCH_GAP="0"):
        from bot.func_helper.emby import _fallback_sessions_endpoint
        H.eq("emby.py 里的兜底实现与 register_throttle 同源（两处不会漂）",
             _fallback_sessions_endpoint(), t.sessions_endpoint())
    note("⚠️ 覆盖缺口：tests/test_terminate_verify.py 与 tests/test_kick_verify_userid.py 把 "
         "endpoint 做了 `split('?')[0]` 归一化，所以这两个套件现在**无法**发现 "
         "`?ActiveWithinSeconds=300` 被改掉/删掉。建议在归一化路由的同时补一条"
         "『记录的会话请求必须带 ActiveWithinSeconds』的断言。")

    section("G2 配置类型转换与兜底")
    with env(EMBY_THROTTLE_BATCH_CONCURRENCY="abc", EMBY_THROTTLE_MIN_INTERVAL_MS="-5",
             EMBY_THROTTLE_BATCH_GAP="3", EMBY_THROTTLE_BREAKER_FAILURES="0",
             EMBY_THROTTLE_ALERT_OWNER="yes"):
        cfg = t.config()
        H.eq("非法 int（abc）→ 回退默认 1", cfg["batch_concurrency"], 1)
        H.eq("负数 min_interval_ms → 夹到 0", cfg["min_interval_ms"], 0)
        H.eq("字符串 '3' → float 3.0", cfg["batch_gap"], 3.0)
        H.eq("breaker_failures=0 → 夹到 1（避免永远不熔断/除零）", cfg["breaker_failures"], 1)
        H.eq("'yes' → True", cfg["alert_owner"], True)
    reset_all()

    section("G3 install() / uninstall() 的彻底性")
    real_req = Embyservice._request
    real_mr = emby.max_retries
    real_pj = rq.RegisterQueueManager._process_job
    real_wc = rq.RegisterQueueManager._configured_worker_count
    t._INSTALLED = False
    t._ORIG_REQUEST = None
    H.eq("install() 首次返回 True", t.install(), True)
    H.eq("install() 重复调用返回 False（幂等）", t.install(), False)
    H.check("Embyservice._request 被替换", Embyservice._request is t._throttled_request)
    H.eq("emby.max_retries 3 -> 2", emby.max_retries, 2)
    H.check("_process_job 被替换", rq.RegisterQueueManager._process_job is t._throttled_process_job)
    H.check("_configured_worker_count 被替换",
            rq.RegisterQueueManager._configured_worker_count is t._capped_worker_count)
    mgr = rq.RegisterQueueManager()
    H.eq("worker 上限真的被压到 register_max_workers=2（config 里写的是 5）",
         mgr._configured_worker_count(), 2)
    H.eq("status()['installed'] == True", t.status()["installed"], True)

    H.eq("uninstall() 返回 True", t.uninstall(), True)
    H.eq("uninstall() 重复调用返回 False", t.uninstall(), False)
    H.check("Embyservice._request 还原", Embyservice._request is real_req)
    H.eq("max_retries 还原成 3", emby.max_retries, real_mr)
    H.check("_process_job 还原", rq.RegisterQueueManager._process_job is real_pj)
    H.check("_configured_worker_count 还原",
            rq.RegisterQueueManager._configured_worker_count is real_wc)
    H.eq("uninstall 后 worker 数回到 config 的 5", mgr._configured_worker_count(), 5)
    H.eq("status()['installed'] == False", t.status()["installed"], False)

    section("G4 request_stop() 的粘滞性 / enter_batch() 的复位 / run_batch() 的缺口")
    reset_all()
    with env(EMBY_THROTTLE_MIN_INTERVAL_MS="0", EMBY_THROTTLE_BATCH_GAP="0"):
        await t.tick()
        H.check("正常 tick() 不抛", True)
        t.request_stop("测试")
        H.eq("is_stopping() == True", t.is_stopping(), True)
        raised = None
        try:
            await t.tick()
        except t.BatchAborted as e:
            raised = str(e)
        H.check("request_stop() 之后 tick() 抛 BatchAborted", raised is not None,
                f"raised={raised!r}")

        async def fresh_batch_with_enter():
            """复刻 syncs.py /restore_from_db 的真实调用方式：循环前 enter_batch()。"""
            t.enter_batch()
            try:
                await t.tick()
                return "started"
            except t.BatchAborted as e:
                return f"aborted:{e}"
            finally:
                t.exit_batch()

        out = await asyncio.create_task(fresh_batch_with_enter())
        note(f"request_stop() 之后，走 enter_batch() 的新批量任务第一次 tick() → {out}")
        H.eq("enter_batch() 会复位遗留的中止标记（真实路径 /restore_from_db 不会一进去就死）",
             out, "started")

        t.request_stop("再测一次")
        rep = await t.run_batch([1, 2, 3], lambda x: asyncio.sleep(0, result=True))
        note(f"request_stop() 之后 run_batch() → {rep.as_text()}")
        H.eq("run_batch() 自己复位遗留的中止标记（修复前会永远 0 处理）", rep.ok, 3)
        H.check("该次 run_batch 没有被中止", not rep.aborted, rep.as_text())
        H.eq("复位后 is_stopping() 也回到 False", t.is_stopping(), False)
        rep2 = await t.run_batch([1, 2, 3], lambda x: asyncio.sleep(0, result=True))
        H.eq("紧接着再跑一次同样正常", rep2.ok, 3)
    reset_all()

    section("G5 热改并发数不再瞬时超配（计数式闸门）")
    with env(EMBY_THROTTLE_BATCH_CONCURRENCY="1", EMBY_THROTTLE_MIN_INTERVAL_MS="0"):
        reset_all()
        c = Counter(delay=0.15)
        with swap_orig(c):
            a = asyncio.create_task(batch_call(c))
            await asyncio.sleep(0.05)
            os.environ["EMBY_THROTTLE_BATCH_CONCURRENCY"] = "3"
            bs = [asyncio.create_task(batch_call(c)) for _ in range(3)]
            await asyncio.gather(a, *bs)
            os.environ["EMBY_THROTTLE_BATCH_CONCURRENCY"] = "1"
        note(f"配置从 1 热改成 3 的过程中，在途峰值 = {c.peak}")
        H.check("热改并发数不会瞬时超配：在途峰值 <= 新配置 3"
                "（修复前是「旧信号量持有者 + 新信号量持有者」叠加，实测 4）",
                c.peak <= 3, f"峰值 {c.peak}（新配置 3）")
        H.check("而且确实放开了（不是把并发锁死）", c.peak >= 2, f"峰值 {c.peak}")
    reset_all()

    section("G6 config() 每次调用都读一次侧车文件（每个 Emby 请求一次）")
    reads = {"n": 0}
    real_read = t._read_sidecar

    def counting_read():
        reads["n"] += 1
        return real_read()

    t._read_sidecar = counting_read
    try:
        N = 20000
        t0 = time.perf_counter()
        for _ in range(N):
            t.config()
        dt = time.perf_counter() - t0
    finally:
        t._read_sidecar = real_read
    H.eq("config() 每次调用恰好读 1 次侧车文件", reads["n"], N)
    note(f"{N} 次 config() 耗时 {dt * 1000:.0f}ms（{dt / N * 1e6:.1f}µs/次），"
         f"侧车文件不存在时是 open() 抛 FileNotFoundError 被吞掉")
    H.check("单次 config() 开销 < 1000µs（不是性能问题，只是量化一下）",
            dt / N * 1e6 < 1000, f"{dt / N * 1e6:.1f}µs")
    note(f"按 2013 个账号 × 3 次请求 = 6039 次请求估算：额外 {6039 * dt / N * 1000:.0f}ms + 6039 次文件系统调用")

    section("G7 _throttled_request 的参数等价性 / request_timeout 的实际生效值")
    p_new = [(n, p.kind.name, p.default) for n, p in
             inspect.signature(t._throttled_request).parameters.items()]
    p_old = [(n, p.kind.name, p.default) for n, p in
             inspect.signature(Embyservice._request).parameters.items()]
    H.eq("签名（参数名 / 种类 / 默认值）与原 _request 完全一致", p_new, p_old)
    H.check("原 _request 的参数就是 (self, method, endpoint, timeout, **kwargs)",
            [n for n, _, _ in p_old] == ["self", "method", "endpoint", "timeout", "kwargs"],
            f"实际 {[n for n, _, _ in p_old]}")

    from bot.schemas.schemas import Open
    eff_to = t.config()["request_timeout"]
    mod_default = t.DEFAULTS["request_timeout"]
    schema_default = Open.model_fields["register_request_timeout"].default
    live_cfg_to = getattr(bot._open, "register_request_timeout", None)
    note(f"request_timeout —— 模块 DEFAULTS={mod_default}s / schemas.Open 默认值={schema_default}s / "
         f"config.json 实际值={live_cfg_to}s / config() 最终生效={eff_to}s")
    H.eq("模块 DEFAULTS 与 schemas.Open 的默认值一致（都是 10s）",
         (mod_default, schema_default), (10.0, 10.0))
    H.check("【配置漂移】线上/本地 config.json 里残留的 15.0 会盖掉代码默认值 10.0，"
            "实际生效仍是 15s → DEFAULTS 注释声称的『最坏 21s』在当前部署上是 31s；"
            "只有把 config.json 的 register_request_timeout 改成 10（或删掉该键）才生效",
            eff_to == 10.0 or float(live_cfg_to or 0) != 10.0,
            f"模块默认={mod_default} schemas默认={schema_default} "
            f"config.json={live_cfg_to} 生效={eff_to}")

    with env(EMBY_THROTTLE_MIN_INTERVAL_MS="0"):
        reset_all()
        c = Counter()
        with swap_orig(c):
            await batch_call(c)
            await interactive_call()
        tos = c.timeouts
        H.check(f"默认（无显式 timeout）时被注入 ClientTimeout(total={eff_to})",
                all(isinstance(x, aiohttp.ClientTimeout) and x.total == eff_to for x in tos),
                f"实际 {tos!r}")
        note(f"原实现里 timeout=None → 不覆盖，用 session 自己的 self.timeout = "
             f"ClientTimeout(total={emby.timeout.total})；现在被强制改成 {eff_to}s。")
        # 显式 timeout 必须原样透传（不被覆盖）
        explicit = aiohttp.ClientTimeout(total=2.5)
        with swap_orig(c):
            await t._throttled_request(None, "GET", "/x", explicit)
        H.check("调用方显式传的 timeout 原样透传（不被覆盖）",
                c.timeouts[-1] is explicit, f"实际 {c.timeouts[-1]!r}")

    section("G8 重试次数改动对 POST 有没有副作用（真 _request + 假 session）")
    attempts = {"n": 0}
    seen_kwargs = []

    class FakeResponse:
        status = 500
        content_type = "application/json"

        async def text(self):
            return "boom"

    class FakeCM:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def request(self, method, url, **kw):
            attempts["n"] += 1
            seen_kwargs.append(kw)
            return FakeCM()

    @asynccontextmanager
    async def fake_session(self):
        yield FakeSession()

    real_session = Embyservice.session
    Embyservice.session = fake_session
    svc = Embyservice.__new__(Embyservice)
    svc.url = "http://127.0.0.1:9"
    svc.headers = {}
    svc.timeout = aiohttp.ClientTimeout(total=10)
    svc.max_retries = 3
    try:
        attempts["n"] = 0
        await svc._request("GET", "/x")
        H.eq("GET + max_retries=3 → 3 次尝试", attempts["n"], 3)

        attempts["n"] = 0
        svc.max_retries = 2
        await svc._request("GET", "/x")
        H.eq("GET + max_retries=2 → 2 次尝试（install() 改的就是这个）", attempts["n"], 2)

        attempts["n"] = 0
        svc.max_retries = 3
        await svc._request("POST", "/x", json={})
        H.eq("【结论】POST + max_retries=3 → 仍然只有 1 次尝试（非幂等不重试）", attempts["n"], 1)

        attempts["n"] = 0
        await svc._request("POST", "/x", json={})
        H.eq("【结论】POST + max_retries=2 → 也是 1 次（改 max_retries 对 POST 零影响）",
             attempts["n"], 1)

        attempts["n"] = 0
        await svc._request("DELETE", "/x")
        H.eq("DELETE 被当作幂等 → 3 次尝试", attempts["n"], 3)
    finally:
        Embyservice.session = real_session
        svc.max_retries = 3

    note("重试预算对比（GET 最坏墙钟）——用**实际生效**的 timeout 算：")
    note(f"  改造前 max_retries=3 / timeout={emby.timeout.total}s → 3×10 + 1 + 2 = 33s")
    note(f"  改造后 max_retries=2 / timeout={eff_to}s → 2×{eff_to:.0f} + 1 = "
         f"{2 * eff_to + 1:.0f}s")
    if eff_to >= 15.0:
        note("  ⚠️ register_throttle.DEFAULTS 里的注释说『刻意保持 10s，最坏 21s』，"
             "但实际生效的是 config.json/schemas 里的 15s → 最坏是 31s，注释与实现不符；"
             "把 schemas.py 的 register_request_timeout 默认值改成 10.0（以及线上 config.json）才能真正拿到 21s。")
    else:
        note("  与 DEFAULTS 注释一致（最坏 21s）。")

    section("G9 熔断状态机：开闸清零 + 冷却到期复位 + 每次开闸都告警（修复后）")
    alerts = []
    real_alert = t._alert_owner

    async def rec_alert(text):
        alerts.append(text)

    t._alert_owner = rec_alert
    try:
        with env(EMBY_THROTTLE_BREAKER_FAILURES="2",
                 EMBY_THROTTLE_BREAKER_COOLDOWN="0.6",
                 EMBY_THROTTLE_MIN_INTERVAL_MS="0",
                 EMBY_THROTTLE_ALERT_OWNER="1"):
            reset_all()
            fail = Counter(ok=False, error="HTTP 500")
            with swap_orig(fail):
                await batch_call(fail)
                await batch_call(fail)
                H.eq("2 次连续失败 → 开闸", t._BREAKER.is_open(), True)
                H.eq("开闸即清零 _fails == 0（修复前保持 2）",
                     t._BREAKER.consecutive_failures, 0)
                for _ in range(3):
                    await interactive_call("/emby/Users/u1")   # 冷却期内的探测失败
                H.eq("冷却期内的失败**不**累积（修复前会涨到 5）",
                     t._BREAKER.consecutive_failures, 0)
                H.eq("冷却期内的失败不会重复开闸（opened_times 仍为 1）",
                     t._BREAKER.opened_times, 1)
            await asyncio.sleep(0)
            H.eq("第一次开闸 → 给 owner 发了 1 条告警", len(alerts), 1)
            await asyncio.sleep(0.8)
            H.eq("冷却到期：is_open() == False", t._BREAKER.is_open(), False)
            H.eq("冷却到期后 _fails 已复位为 0（修复前仍是 5）",
                 t._BREAKER.consecutive_failures, 0)
            with swap_orig(fail):
                await batch_call(fail)
                H.eq("冷却到期后 1 次失败**不**会立刻重新开闸（阈值 2 仍然有效）",
                     t._BREAKER.is_open(), False)
                H.eq("opened_times 仍是 1", t._BREAKER.opened_times, 1)
                await batch_call(fail)
                H.eq("再累积满 2 次才重新开闸", t._BREAKER.is_open(), True)
                H.eq("opened_times 变成 2", t._BREAKER.opened_times, 2)
            await asyncio.sleep(0)
            H.eq("第二次开闸同样给 owner 发了告警（修复前第二次起全程静默）",
                 len(alerts), 2)
            reset_all()
            # 成功一次之后 _alerted 复位，下一轮故障又能告警
            okc = Counter(ok=True)
            await asyncio.sleep(0.7)
            with swap_orig(okc):
                await batch_call(okc)
            H.eq("成功一次后 _alerted 复位", t._BREAKER._alerted, False)
    finally:
        t._alert_owner = real_alert
        reset_all()

    section("G10 _pace 锁内只预约、sleep 在锁外 → 不再跨 lane 串行")
    with env(EMBY_THROTTLE_MIN_INTERVAL_MS="250",
             EMBY_THROTTLE_INTERACTIVE_MIN_INTERVAL_MS="250",
             EMBY_THROTTLE_BATCH_CONCURRENCY="2"):
        reset_all()
        c = Counter(delay=0.0)
        with swap_orig(c):
            await batch_call(c)                       # 第 1 次：建立 _pace_last["batch"]
            hog = asyncio.create_task(batch_call(c))  # 第 2 次：会 sleep 250ms（但已不持锁）
            await asyncio.sleep(0.03)
            t0 = asyncio.get_running_loop().time()
            await interactive_call()                  # 自己的 lane 之前没人，本应立刻通过
            dt = asyncio.get_running_loop().time() - t0
            await hog
        note(f"interactive 自己的 lane 没有前序请求，实际等待 {dt * 1000:.0f}ms")
        H.check("interactive 不再被 batch lane 的 pacing 阻塞"
                "（_pace 锁内只预约时间点，sleep 在锁外）",
                dt < 0.15, f"实际等待 {dt * 1000:.0f}ms")
        note("修复前是「持锁 sleep」，interactive 即使自己不需要限速也要排队等锁，"
             "实测被拖 221ms。")
    reset_all()

    section("G12 tick() 不再与 _throttled_request 双重 pacing（/restore_from_db 少一个节拍）")
    with env(EMBY_THROTTLE_MIN_INTERVAL_MS="100", EMBY_THROTTLE_BATCH_GAP="0",
             EMBY_THROTTLE_BATCH_CONCURRENCY="1"):

        async def run_accounts(use_tick, n=3):
            reset_all()
            async def timed(self_obj, method, endpoint, timeout=None, **kw):
                return SimpleNamespace(success=True, error=None, data={})
            with swap_orig(timed):
                t.enter_batch()
                try:
                    t0 = asyncio.get_running_loop().time()
                    for _ in range(n):
                        if use_tick:
                            await t.tick()
                        for ep in ("/emby/Users/New", "/emby/Users/x/Password",
                                   "/emby/Users/x/Policy"):
                            await t._throttled_request(None, "POST", ep)
                    return asyncio.get_running_loop().time() - t0
                finally:
                    t.exit_batch()

        with_tick = await run_accounts(True)
        no_tick = await run_accounts(False)
        note(f"3 个账号 × 3 次请求，min_interval=100ms：带 tick {with_tick * 1000:.0f}ms / "
             f"不带 tick {no_tick * 1000:.0f}ms")
        note("修复前 tick() 自己也调一次 _pace('batch')，与请求层的节拍共用 _pace_last['batch']，"
             "每号占 4 拍（1+3）→ 线上 400ms 时每号 1.6s 而不是 1.2s，恢复 2013 个号白多花约 13 分钟。")
        H.ge("9 个节拍 → 8 次 sleep ≈ 0.8s（请求层仍然限速）", no_tick, 0.7)
        H.check("tick() 不再额外占 pacing 节拍：带 tick 与不带 tick 耗时基本一致",
                abs(with_tick - no_tick) < 0.15,
                f"{with_tick:.3f}s vs {no_tick:.3f}s")
        H.check("带 tick 也不再有 12 拍（≈1.1s）", with_tick < 0.95, f"{with_tick:.3f}s")
    reset_all()

    section("G11 emby_create_safe 已改成 emby.emby_create 的薄封装")
    src = inspect.getsource(t.emby_create_safe)
    body = src.split('"""')[-1]        # 去掉 docstring，只看真正的代码
    H.check("源码里只有转发（出现 _emby.emby_create，且没有第二份策略计算）",
            "_emby.emby_create" in body and "create_policy" not in body
            and "policy.update" not in body and "EnabledFolders" not in body,
            body.strip()[:160])
    saved_req = emby._request
    saved_del = emby._delete_orphan_account
    router = FakeEmbyHTTP(libs=REAL_ONLINE_LIBS)
    emby._request = router

    async def fake_del(user_id, name=None):
        return None

    emby._delete_orphan_account = fake_del

    async def empty_cvf(ttl=None, force=False):
        return {}

    real_cvf = t.cached_virtual_folders
    t.cached_virtual_folders = empty_cvf
    try:
        res = await t.emby_create_safe("验证用户F", 30)
    finally:
        t.cached_virtual_folders = real_cvf
        emby._request = saved_req
        emby._delete_orphan_account = saved_del

    note(f"emby_create_safe 在媒体库列表为空时的返回值：{res!r}")
    note(f"写入的 Policy 体（只列关键字段）："
         f"{[{k: v for k, v in b.items() if k in ('EnableAllFolders', 'EnabledFolders', 'BlockedMediaFolders')} for b in router.policy_bodies]}")
    H.check("空库时建号仍成功（不抛异常）", isinstance(res, tuple) and len(res) == 3,
            f"实际 {res!r}")
    H.eq("空库时没有写出锁死用户的策略（继承 emby_create 的空库守卫）",
         lockout_policies(router), [])
    H.check("空库时走了回退路径（请求数 > 3）", len(router.calls) > 3,
            f"请求数 {len(router.calls)}")
    note("（原实现自己复制了一份策略计算、无空库守卫，会写出 EnableAllFolders=False + "
         "EnabledFolders=[] 把用户锁死；Lead 已把它改成薄封装，这条缺陷已消失。）")


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# H 差分验证：改造前版 emby_create  vs  当前版 emby_create（甲/乙两支都跑）
#
# 基准是**钉死的一个提交**（BASE_COMMIT），不是 HEAD —— 这一点很关键：
# 本次改动一旦提交，HEAD 里就有 _build_full_policy 了，再拿 HEAD 当基准
# 等于「拿新代码跟自己比」，整个 H 段会静默变成永远通过的空测试。
# 基准优先读仓库里的冻结副本 tests/fixtures/emby_baseline_<sha>.py（CI 是浅克隆，
# 取不到历史提交），取不到时回退 `git show <sha>:bot/func_helper/emby.py`。
# ══════════════════════════════════════════════════════════════════════════════

# Emby `UserPolicy` 构造函数的默认值（本项目 create_policy 依赖的就是它们）。
# 差分对比里 基准 与当前版用**同一套**建模，所以它不影响结论；
# 影响结论的只有 EnableAllFolders 的默认值（True），而这一点是由
# create_policy() 故意不写这两个字段的事实反推得到的。
EMBY_USER_POLICY_DEFAULTS = {
    "IsAdministrator": False,
    "IsHidden": False,
    "IsHiddenRemotely": True,
    "IsDisabled": False,
    "EnableRemoteControlOfOtherUsers": False,
    "EnableSharedDeviceControl": True,
    "EnableRemoteAccess": True,
    "EnableLiveTvManagement": True,
    "EnableLiveTvAccess": True,
    "EnableMediaPlayback": True,
    "EnableAudioPlaybackTranscoding": True,
    "EnableVideoPlaybackTranscoding": True,
    "EnablePlaybackRemuxing": True,
    "EnableContentDeletion": False,
    "EnableContentDownloading": True,
    "EnableSubtitleDownloading": True,
    "EnableSubtitleManagement": False,
    "EnableSyncTranscoding": True,
    "EnableMediaConversion": False,
    "EnableAllDevices": True,
    "EnableAllFolders": True,
    "EnabledFolders": [],
    "SimultaneousStreamLimit": 0,
    "BlockedMediaFolders": [],
    "AllowCameraUpload": False,
    "EnabledDevices": [],
    "BlockedChannels": [],
    "EnabledChannels": [],
    "EnabledTags": [],
    "BlockedTags": [],
    "RemoteClientBitrateLimit": 0,
}


# 改造前的最后一个提交（本次限流改造的父提交）。差分对照钉在这个 sha 上。
BASE_COMMIT = "a587509"
BASELINE_FIXTURE = ROOT / "tests" / "fixtures" / f"emby_baseline_{BASE_COMMIT}.py"


def load_baseline_module():
    """
    把**改造前**的 emby.py 当独立模块加载（拿真代码当基准，不是我的复述）。

    读取顺序（CI 是浅克隆，取不到历史提交，所以必须有冻结副本）：
      1. tests/fixtures/emby_baseline_<BASE_COMMIT>.py  —— 冻结副本，byte 级等同原文件
      2. `git show <BASE_COMMIT>:bot/func_helper/emby.py` —— 完整克隆时的真身
    两个都拿不到就抛异常，让这一整段显式失败 —— 绝不允许静默退回 HEAD
    （那会让「新旧对比」变成「新和新对比」，测试永远通过而毫无意义）。
    """
    import subprocess
    import types

    if BASELINE_FIXTURE.is_file():
        src = BASELINE_FIXTURE.read_text(encoding="utf-8")
        label = f"tests/fixtures/{BASELINE_FIXTURE.name}"
    else:
        label = f"{BASE_COMMIT}:bot/func_helper/emby.py"
        try:
            src = subprocess.check_output(
                ["git", "show", f"{BASE_COMMIT}:bot/func_helper/emby.py"],
                cwd=str(ROOT), stderr=subprocess.DEVNULL,
            ).decode("utf-8")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"取不到改造前的 emby.py 基准：既没有 {BASELINE_FIXTURE}，"
                f"也 `git show {BASE_COMMIT}:bot/func_helper/emby.py` 失败（{e}）。"
                "浅克隆下请保留冻结副本。"
            ) from e

    mod = types.ModuleType("baseline_emby")
    mod.__file__ = label
    sys.modules["baseline_emby"] = mod
    exec(compile(src, label, "exec"), mod.__dict__)
    return mod


class FakeEmbyServer:
    """
    把 Emby 侧「存策略」的语义建模出来，用于比较 基准 / 快路径的**最终生效值**。

    model="reset"：Emby 真实语义 —— body 反序列化成 UserPolicy 对象，
                   缺的字段回落成构造函数默认值；
    model="merge"：缺的字段保留原值。
    两种模型都跑，结论不依赖建模选择。
    """

    def __init__(self, libs, model="reset", user_id="u-new-1"):
        self.libs = libs
        self.model = model
        self.user_id = user_id
        self.users = {}
        self.calls = []
        self.policy_bodies = []

    def _store(self, body):
        if self.model == "reset":
            stored = dict(EMBY_USER_POLICY_DEFAULTS)
            stored.update(body)
        else:
            stored = dict(self.users[self.user_id])
            stored.update(body)
        self.users[self.user_id] = stored

    async def request(self, method, endpoint, timeout=None, **kwargs):
        self.calls.append((method, endpoint))
        if endpoint == "/emby/Users/New":
            self.users[self.user_id] = dict(EMBY_USER_POLICY_DEFAULTS)
            return EmbyApiResult(True, {"Id": self.user_id})
        if endpoint.endswith("/Password"):
            return EmbyApiResult(True, {})
        if endpoint.endswith("/Policy"):
            body = kwargs.get("json") or {}
            self.policy_bodies.append(body)
            self._store(body)
            return EmbyApiResult(True, {})
        if endpoint == "/emby/Library/VirtualFolders":
            return EmbyApiResult(True, self.libs)
        if endpoint.startswith("/emby/Users/"):
            return EmbyApiResult(True, {"Id": self.user_id,
                                        "Policy": dict(self.users[self.user_id])})
        return EmbyApiResult(False, error=f"未预期的端点 {endpoint}")

    def visible_guids(self):
        p = self.users[self.user_id]
        if p.get("EnableAllFolders") is True:
            return {lib["Guid"] for lib in self.libs if lib.get("Guid")}
        return set(p.get("EnabledFolders") or [])

    def visible_names(self):
        by_guid = {lib["Guid"]: lib["Name"] for lib in self.libs if lib.get("Guid")}
        return {by_guid.get(g, g) for g in self.visible_guids()}


def _norm_policy(p):
    out = dict(p)
    bm = out.get("BlockedMediaFolders")
    if isinstance(bm, list):
        out["BlockedMediaFolders"] = sorted(bm)
    ef = out.get("EnabledFolders")
    if isinstance(ef, list):
        out["EnabledFolders"] = list(ef)
    return out


async def _run_head(base_mod, server, name="差分用户"):
    svc = base_mod.Embyservice.__new__(base_mod.Embyservice)
    svc._request = server.request

    async def fake_del(emby_id):
        return True

    svc.emby_del = fake_del
    return await svc.emby_create(name, 30)


async def _run_current(server, libs, name="差分用户"):
    svc = new_svc(server.request)
    real_cvf = t.cached_virtual_folders

    async def _cvf(ttl=None, force=False):
        return {lib["Guid"]: lib["Name"] for lib in libs}

    t.cached_virtual_folders = _cvf
    try:
        return await svc.emby_create(name, 30)
    finally:
        t.cached_virtual_folders = real_cvf


async def test_H_differential():
    section(f"H0 加载改造前的基准（{BASE_COMMIT}，冻结副本或 git show）")
    try:
        base_mod = load_baseline_module()
        H.check("改造前 emby.py 作为独立模块加载成功", True)
    except Exception as e:
        H.check("改造前 emby.py 作为独立模块加载成功", False, f"{type(e).__name__}: {e}")
        return
    H.check("基准版有 emby_create", hasattr(base_mod.Embyservice, "emby_create"))
    H.check("基准版有 hide_folders_by_names", hasattr(base_mod.Embyservice, "hide_folders_by_names"))
    # 这条是「基准没被钉错」的哨兵：基准里必须**没有** _build_full_policy。
    # 一旦有人把基准指回 HEAD（也就是指回新代码），这里立刻变红，
    # 否则整个 H 段会退化成「新代码 vs 新代码」的永远通过。
    H.check("基准版**没有** _build_full_policy（说明基准确实钉在改造前）",
            not hasattr(base_mod.Embyservice, "_build_full_policy"))

    cases = [
        ("分支甲（线上真实库名，一个都对不上）", REAL_ONLINE_LIBS, 6),
        ("分支乙（'nsfw' / '电视' 都能对上）", FakeEmbyHTTP.LIBS, 8),
        ("分支乙'（只有 extra 库『电视』能对上）", [
            {"Guid": "g-guochan", "Name": "⚔️国产·动漫"},
            {"Guid": "g-tv", "Name": "电视"},
            {"Guid": "g-playlist", "Name": "播放列表"},
        ], 8),
    ]

    for model in ("reset", "merge"):
        for label, libs, expect_base_calls in cases:
            section(f"H1 [{model}] {label}")
            srv_h = FakeEmbyServer(libs, model=model)
            srv_c = FakeEmbyServer(libs, model=model)
            res_h = await _run_head(base_mod, srv_h)
            res_c = await _run_current(srv_c, libs)

            note(f"基准   请求数={len(srv_h.calls)} 路径={[e for _, e in srv_h.calls]}")
            note(f"快路径 请求数={len(srv_c.calls)} 路径={[e for _, e in srv_c.calls]}")
            ph = srv_h.users[srv_h.user_id]
            pc = srv_c.users[srv_c.user_id]
            note(f"基准   最终 EnableAllFolders={ph.get('EnableAllFolders')!r} "
                 f"EnabledFolders={ph.get('EnabledFolders')!r}")
            note(f"快路径 最终 EnableAllFolders={pc.get('EnableAllFolders')!r} "
                 f"EnabledFolders={pc.get('EnabledFolders')!r}")
            note(f"基准   可见库={sorted(srv_h.visible_names())}")
            note(f"快路径 可见库={sorted(srv_c.visible_names())}")
            note(f"基准   写入的 Policy 次数={len(srv_h.policy_bodies)} "
                 f"快路径={len(srv_c.policy_bodies)}")

            H.check("两边都建号成功",
                    isinstance(res_h, tuple) and isinstance(res_c, tuple),
                    f"基准={res_h!r} 快路径={res_c!r}")
            H.eq(f"基准 请求数 == {expect_base_calls}（基线，Lead 给的是 {expect_base_calls}）",
                 len(srv_h.calls), expect_base_calls)
            H.eq("快路径请求数 == 3", len(srv_c.calls), 3)
            H.eq("EnableAllFolders 最终值一致",
                 pc.get("EnableAllFolders"), ph.get("EnableAllFolders"))
            H.eq("EnabledFolders 最终值一致（含顺序）",
                 list(pc.get("EnabledFolders") or []), list(ph.get("EnabledFolders") or []))
            H.eq("BlockedMediaFolders 最终值一致（按集合比）",
                 sorted(pc.get("BlockedMediaFolders") or []),
                 sorted(ph.get("BlockedMediaFolders") or []))
            if sorted(pc.get("BlockedMediaFolders") or []) != list(pc.get("BlockedMediaFolders") or []):
                note("（BlockedMediaFolders 的**列表顺序**两边不同：基准 走 list(set(...)) 的哈希序，"
                     "快路径是确定序；集合完全相同，Emby 不关心顺序）")
            H.eq("【核心】可见媒体库集合逐字一致", srv_c.visible_names(), srv_h.visible_names())
            H.eq("整份最终策略一致（Blocked 归一化后）", _norm_policy(pc), _norm_policy(ph))

    section("H2 _build_full_policy 的分支判定（直接调用，不经过 HTTP）")
    policy_jia = Embyservice._build_full_policy({lib["Guid"]: lib["Name"] for lib in REAL_ONLINE_LIBS})
    H.check("分支甲：不含 EnableAllFolders 键", "EnableAllFolders" not in policy_jia,
            f"keys={sorted(policy_jia)}")
    H.check("分支甲：不含 EnabledFolders 键", "EnabledFolders" not in policy_jia,
            f"keys={sorted(policy_jia)}")
    H.eq("分支甲：BlockedMediaFolders == ['播放列表','电视']",
         policy_jia.get("BlockedMediaFolders"), ["播放列表", "电视"])
    policy_yi = Embyservice._build_full_policy({lib["Guid"]: lib["Name"] for lib in FakeEmbyHTTP.LIBS})
    H.eq("分支乙：EnableAllFolders == False", policy_yi.get("EnableAllFolders"), False)
    H.eq("分支乙：EnabledFolders == ['g-movie','g-playlist']", policy_yi.get("EnabledFolders"),
         ["g-movie", "g-playlist"])
    empty = Embyservice._build_full_policy({})
    H.check("空库（上游有守卫，理论上走不到）也不会写出空 EnabledFolders 锁死策略",
            not (empty.get("EnableAllFolders") is False and empty.get("EnabledFolders") == []),
            f"{empty.get('EnableAllFolders')!r} / {empty.get('EnabledFolders')!r}")


async def main():
    print("=" * 78)
    print("register_throttle / 建号快路径 / 注册队列 —— 独立验证")
    print("=" * 78)
    note(f"register_throttle 配置快照：{t.config()}")
    note(f"侧车文件 register_throttle.json 存在？{os.path.exists(t.SIDECAR_NAME)}")

    await test_A_gate()
    await test_B_breaker()
    await test_C_create_request_count()
    await test_D_vf_cache()
    await test_E_queue()
    await test_F_stress()
    await test_G_adversarial()
    await test_H_differential()

    print()
    print("=" * 78)
    print(f"  结果：PASS={H.passed}  FAIL={H.failed}")
    print("=" * 78)
    if H.failures:
        print("失败明细：")
        for name, detail in H.failures:
            print(f"  - {name}")
            if detail:
                print(f"      {detail}")
    return 1 if H.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
