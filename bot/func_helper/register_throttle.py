#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
register_throttle —— Emby 客户端侧「安全批量建号」统一限流闸门（**纯新增模块**）

背景
────
线上 3 号机（embyboss 容器）在同一时间段批量建号时，Emby（https://emby.teawaya.com，
官方 4.10.0.40，ServerName=ChaPanda）会整体假死：HTTP 全部超时，只能重启容器恢复。

客户端侧的三个放大器（详见 artifacts/bot-side-patch.md 的证据章节）：

1. **单账号 7 次 HTTP 往返**（4 次写 + 3 次读），其中 `POST /Users/{id}/Policy`
   被写了两次、`GET /Users/{id}` 被读了两次、`GET /Library/VirtualFolders` 每账号重复拉取；
2. **没有全局并发闸门**：`register_worker_count=5` 的 5 个 worker + 不受队列约束的
   `/restore_from_db`（串行遍历 2000+ 用户，零间隔）+ 批量媒体库命令（同一批用户的
   读-改-写策略）可以同时打同一台 Emby；
3. **重试无熔断**：`_request` 对幂等方法做 3 次重试、超时 10s、退避 1/2/4s 且几乎无抖动，
   单个 `GET /emby/Sessions` 最坏要挂 10+1+10+2+10 = **33 秒**才放弃（线上日志
   11:33:47 → 11:33:59 → 11:34:12 的 12s/13s 间隔与之一致）。
   Emby 越慢 → 超时越多 → 重试越多 → Emby 更慢，形成自我放大。

本模块做四件事（**全部通过运行时挂载实现，不需要改仓库里的任何原文件**）：

* `ThrottleGate`：全局并发闸门（批量 lane 默认 1）+ 最小请求间隔 + 可热调参；
* `CircuitBreaker`：连续失败熔断暂停（默认连续 5 次 / 暂停 60s）+ 告警；
* 熔断期间把**所有** Emby 请求降级成「快速探测」（默认 3s 超时），
  把 33 秒的挂死压缩到个位数秒，避免巡检任务把已经卡住的 Emby 继续拖住；
* `run_batch()/tick()`：批量任务分片（默认 10 个/批，批间隔 20s）+ 可中断（`request_stop()`）。

安装（任选一种，见 bot-side-patch.md）
────────────────────────────────────
A. 在 `main.py` 顶部加两行：
       import bot.func_helper.register_throttle as register_throttle
       register_throttle.install()
B. 完全不碰仓库文件，只改 docker-compose 的 command：
       command: ["-c", "import bot.func_helper.register_throttle as t; t.install(); "
                       "import runpy; runpy.run_path('main.py', run_name='__main__')"]

配置来源（三级回退，**全部可选，缺失即用默认值**）
──────────────────────────────────────────────────
1. 环境变量 `EMBY_THROTTLE_<KEY大写>`，例如 `EMBY_THROTTLE_BATCH_CONCURRENCY=1`
2. 侧车文件 `register_throttle.json`（放在 config.json 同目录，**支持热加载**，改完即生效）
3. `_open` 上的同名属性（需要在 bot/schemas/schemas.py 的 Open 里声明，见可选补丁）
4. 内置默认值 `DEFAULTS`

⚠️ 为什么不直接用 config.json 的 "open" 段？
   `Open(BaseModel)` 没有开 `extra="allow"`，pydantic v2 默认 `extra="ignore"`：
   往 config.json 里加未声明的键，**读不到（getattr 只会拿到默认值）而且下次
   `save_config()` 会把它从 config.json 里静默抹掉**。所以侧车文件/环境变量是
   唯一「不改 schema 就能调参」的路子。想写进 config.json 就必须一起打
   schemas.py 的可选补丁。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import aiohttp

__all__ = [
    "DEFAULTS",
    "BatchAborted",
    "install",
    "uninstall",
    "config",
    "status",
    "batch_lane",
    "enter_batch",
    "exit_batch",
    "tick",
    "request_stop",
    "clear_stop",
    "is_stopping",
    "run_batch",
    "BatchReport",
    "emby_create_safe",
    "cached_virtual_folders",
    "invalidate_virtual_folders",
    "sessions_endpoint",
]

SIDECAR_NAME = "register_throttle.json"
ENV_PREFIX = "EMBY_THROTTLE_"

# 所有可调项及默认值。键名同时用于：环境变量后缀、侧车 json 键、_open 属性名（原样）。
DEFAULTS: Dict[str, Any] = {
    # 总开关（关掉=完全回到原行为，仅保留日志）
    "enabled": True,
    # 建号/批量 lane 的全局并发上限（闸门第一层）
    "batch_concurrency": 1,
    # 交互/巡检 lane 的并发上限（防止面板查询把批量挤爆，或反过来）
    "interactive_concurrency": 6,
    # 覆盖 register_worker_count 的**上限**（只限制增长；缩容需改 config + 重启）
    "max_workers": 2,
    # 批量 lane 内两次请求之间的最小间隔（毫秒）
    # 400ms：建 1 个号 3 次请求 ≈ 1.2s（约 40~50 个/分钟）。Emby 单请求健康时
    # 只要 5~30ms，这个节奏对它几乎没有压力；要更保守就提到 800，要极限可降到 150。
    "min_interval_ms": 400,
    # 交互/巡检 lane 的最小间隔（毫秒，默认 0=不额外加延迟）
    "interactive_min_interval_ms": 0,
    # 批量任务分片大小（每批多少个账号）
    "shard_size": 25,
    # 分片之间的间隔（秒）：每号额外摊 0.4s，换来 Emby 每 10 秒喘一口气
    "batch_gap": 10.0,
    # 连续失败多少次触发熔断
    "breaker_failures": 5,
    # 熔断暂停时长（秒）
    "breaker_cooldown": 60.0,
    # 单请求超时（秒）。**刻意保持与原实现一致的 10s，不放宽。**
    # 理由：放宽到 15s 会让「最坏墙钟」从 2×10+1 = 21s 涨到 2×15+1 = 31s，
    # 交互/巡检请求也会跟着多挂 5 秒。而且超时本来就是熔断器的输入信号 ——
    # Emby 真的慢就该早点失败、早点熔断，而不是让机器人的 worker 陪它一起等。
    # 线上实测 Emby 健康时单请求 5~30ms，卡的时候劣化到 0.5~6.5s，10s 有足够余量。
    "request_timeout": 10.0,
    # 幂等请求（GET 等）的重试次数（含首次）。原实现 3 → 2，
    # 单个 GET 最坏墙钟从 3×10+1+2 = 33s 压到 2×10+1 = 21s。
    "retries": 2,
    # 熔断期间的"快速探测"超时（秒）
    "probe_timeout": 3.0,
    # 熔断时是否私聊 owner 告警
    "alert_owner": True,
    # GET /Library/VirtualFolders 的缓存 TTL（秒）
    "virtualfolders_ttl": 300.0,
    # run_batch 在熔断时是等待冷却后继续（False）还是直接中止（True）
    "abort_on_breaker": False,
    # run_batch 单项失败后的额外重试次数（指数退避）
    "item_retries": 1,
    # 指数退避基准/上限（秒）：第 n 次重试前 sleep min(cap, base * 2**n) + 0~0.3s 抖动
    "backoff_base": 1.0,
    "backoff_cap": 8.0,
    # GET /emby/Sessions 的 ActiveWithinSeconds。
    # 线上实测：不带这个参数返回 2,970,545 B / 4037 条（其中真正在播只有 7~8 条），
    # 耗时 7.7s；带 300 后是 89,940 B / 75 条 / 0.66s —— 体积 1/33、耗时 1/12。
    # 正在播放的会话每 10s 就上报一次进度，绝不会被 300s 的窗口漏掉。
    # 设为 0 表示不加该参数（回到旧行为）。
    "session_active_seconds": 300,
}

_BOOL_KEYS = {"enabled", "alert_owner", "abort_on_breaker"}
_INT_KEYS = {"batch_concurrency", "interactive_concurrency", "max_workers",
             "min_interval_ms", "interactive_min_interval_ms", "shard_size",
             "breaker_failures", "retries", "item_retries", "session_active_seconds"}
_FLOAT_KEYS = {"batch_gap", "breaker_cooldown", "request_timeout", "probe_timeout",
               "virtualfolders_ttl", "backoff_base", "backoff_cap"}

# `config()` 去 `_open`（= config.json 的 "open" 段）上找字段名时的**例外表**。
# 默认规则是「先试 `register_<key>`，再试裸 `<key>`」，但总开关对不上：
#   key = "enabled" → 默认只会试 register_enabled / enabled，
#   而 schema（bot/schemas/schemas.py）里的字段名是 **register_throttle_enabled**。
# 后果曾经很隐蔽：config.json 里写 `"register_throttle_enabled": false` 完全无效，
# 限流照跑（间隔/并发/熔断全生效），只有环境变量 EMBY_THROTTLE_ENABLED=0 能关。
# 这里按优先级列出所有可接受的名字，兼容旧写法。
_OPEN_ALIASES = {
    "enabled": ("register_throttle_enabled", "register_enabled", "enabled"),
}


# ──────────────────────────────────────────────────────────────────────────────
# 配置解析
# ──────────────────────────────────────────────────────────────────────────────

def _coerce(key: str, value: Any) -> Any:
    """把环境变量/侧车 json 里的原始值转成目标类型；转不了就返回 None（用默认值）。"""
    try:
        if key in _BOOL_KEYS:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on", "y")
        if key in _INT_KEYS:
            return int(float(value))
        if key in _FLOAT_KEYS:
            return float(value)
        return value
    except (TypeError, ValueError):
        return None


def _read_sidecar() -> Dict[str, Any]:
    """读侧车文件。文件不存在/坏了都不抛异常（限流模块绝不能把 bot 弄起不来）。"""
    try:
        with open(SIDECAR_NAME, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def config() -> Dict[str, Any]:
    """
    解析当前生效配置（每次调用都重新读侧车文件，所以改文件即生效，无需重启）。

    优先级：环境变量 > 侧车文件 > _open 属性（`register_<key>` 或 `<key>`）> 默认值。
    """
    out = dict(DEFAULTS)

    # 3) _open 属性（只有 schemas 里声明过才拿得到）
    try:
        from bot import _open as _open_obj
    except Exception:
        _open_obj = None
    if _open_obj is not None:
        for key in DEFAULTS:
            # 优先读 `register_<key>`（写进 config.json "open" 段时用的名字，
            # 见 bot/schemas/schemas.py 的可选补丁），其次读裸 `<key>`。
            #
            # ⚠️ `enabled` 有例外：schema 里的字段名是 `register_throttle_enabled`
            # （见 _OPEN_ALIASES 的注释），`register_enabled` / `enabled` 都不存在。
            # 曾经因为这里只试 `register_{key}` / `{key}`，导致
            # **config.json 里写 `register_throttle_enabled: false` 根本关不掉限流**
            # （永远吃 DEFAULTS 的 True，只有环境变量 EMBY_THROTTLE_ENABLED=0 有用）。
            for attr in _OPEN_ALIASES.get(key, (f"register_{key}", key)):
                if hasattr(_open_obj, attr):
                    val = _coerce(key, getattr(_open_obj, attr))
                    if val is not None:
                        out[key] = val
                        break

    # 2) 侧车文件
    for key, raw in _read_sidecar().items():
        if key in DEFAULTS:
            val = _coerce(key, raw)
            if val is not None:
                out[key] = val

    # 1) 环境变量
    for key in DEFAULTS:
        env = os.environ.get(ENV_PREFIX + key.upper())
        if env is not None and env != "":
            val = _coerce(key, env)
            if val is not None:
                out[key] = val

    # 兜底修正，避免把并发配成 0 或负数导致永久死锁
    out["batch_concurrency"] = max(1, int(out["batch_concurrency"]))
    out["interactive_concurrency"] = max(1, int(out["interactive_concurrency"]))
    out["max_workers"] = max(1, int(out["max_workers"]))
    out["shard_size"] = max(1, int(out["shard_size"]))
    out["breaker_failures"] = max(1, int(out["breaker_failures"]))
    out["retries"] = max(1, int(out["retries"]))
    out["min_interval_ms"] = max(0, int(out["min_interval_ms"]))
    out["interactive_min_interval_ms"] = max(0, int(out["interactive_min_interval_ms"]))
    out["item_retries"] = max(0, int(out["item_retries"]))
    out["backoff_base"] = max(0.0, float(out["backoff_base"]))
    out["backoff_cap"] = max(0.0, float(out["backoff_cap"]))
    out["session_active_seconds"] = max(0, int(out["session_active_seconds"]))
    return out


def sessions_endpoint() -> str:
    """
    会话列表端点（**建议所有查会话的调用点都用它**）。

    为什么必须带 ActiveWithinSeconds
    ────────────────────────────────
    线上这台 Emby 的 `/emby/Sessions` 实测返回 2,970,545 B / 4037 条 session，
    其中真正 `NowPlayingItem` 非空的只有 7~8 条 —— 也就是 4000 多条永不清理的
    僵尸会话（只在进程内存里，重启才归零）。机器人每分钟拉一次，等于每分钟往
    Emby 的 18GB 托管堆上砸一次 3MB 的序列化分配，是「假死」的固定放大器之一。

    加上 `?ActiveWithinSeconds=300` 后：89,940 B / 75 条 / 0.66s（体积 1/33）。
    正在播放的会话每 10 秒就上报一次进度，300 秒窗口不可能漏掉它。

    返回示例：`/emby/Sessions?ActiveWithinSeconds=300`；配置为 0 时退回原路径。
    """
    try:
        seconds = int(config().get("session_active_seconds", 300))
    except Exception:
        seconds = 300
    if seconds <= 0:
        return "/emby/Sessions"
    return f"/emby/Sessions?ActiveWithinSeconds={seconds}"


def _now() -> float:
    """
    单调时钟。优先用事件循环时钟；**在事件循环之外调用时回退到 time.monotonic()**，
    这样 `status()` / `is_open()` 也能在同步上下文（管理面板、诊断脚本）里安全调用。
    """
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()


def _logger():
    try:
        from bot import LOGGER
        return LOGGER
    except Exception:  # pragma: no cover - 极端导入顺序下退化
        import logging
        return logging.getLogger("register_throttle")


# ──────────────────────────────────────────────────────────────────────────────
# lane / 闸门 / 节奏
# ──────────────────────────────────────────────────────────────────────────────

# 当前任务是否属于"批量建号"lane。contextvars 按任务隔离，
# 所以用 enter_batch() 打开后，该任务里**所有** Emby 请求都会走批量闸门。
_BATCH_LANE: contextvars.ContextVar[bool] = contextvars.ContextVar("emby_batch_lane", default=False)

_STOP = asyncio.Event()


class _Gate:
    """
    计数式并发闸门（替代 `asyncio.Semaphore`），支持**热改容量而不瞬时超配**。

    为什么不用 Semaphore：改容量只能换对象，而旧对象上的持有者不会退出，
    于是「旧持有者的在途数 + 新对象新放行的数」叠加 —— 实测配置 1→3 的过程中
    在途峰值 = 4，等于"改配置 = 瞬间放宽"，Emby 会白挨一路并发；反复改还会累积。

    这里用「已占用数 + 条件变量」，容量在每次进入时现读（由 `_semaphores()` 刷新）：
      · 扩容：立刻多放行（本来就没超配）
      · 缩容：等到在途数降到新容量以下才继续放行 —— **绝不超配**
    """

    def __init__(self, name: str):
        self._name = name
        self._in_flight = 0
        self._want = 1
        self._cond = asyncio.Condition()

    def set_capacity(self, n: int) -> None:
        self._want = max(1, int(n))

    @property
    def capacity(self) -> int:
        return self._want

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def __aenter__(self):
        async with self._cond:
            while self._in_flight >= self._want:
                await self._cond.wait()
            self._in_flight += 1
        return self

    async def __aexit__(self, *exc):
        async with self._cond:
            self._in_flight -= 1
            self._cond.notify_all()
        return False


_gate_batch = _Gate("batch")
_gate_interactive = _Gate("interactive")

_pace_lock = asyncio.Lock()
_pace_last: Dict[str, float] = {}

# tick() 的分片计数器
_tick_lock = asyncio.Lock()
_tick_state = {"count": 0, "last": 0.0}


async def _semaphores(cfg: Dict[str, Any]) -> Tuple[_Gate, _Gate]:
    """把最新并发数刷进两条 lane 的闸门并返回它们。

    这里**不再重建对象**（重建就是"旧持有者 + 新持有者"叠加的根源），
    只更新容量，由 `_Gate` 自己保证在途数不会超过新容量。
    """
    _gate_batch.set_capacity(int(cfg["batch_concurrency"]))
    _gate_interactive.set_capacity(int(cfg["interactive_concurrency"]))
    return _gate_batch, _gate_interactive


async def _pace(lane: str, interval_ms: int):
    """同 lane 内保证两次请求间隔 >= interval_ms（用事件循环时钟，不受系统时间跳变影响）。

    实现要点：锁内**只预约下一个时间点**，`sleep` 放到锁外。
    原实现是 `async with _pace_lock:` 里直接 `await asyncio.sleep(wait)` ——
    结果是另一条 lane 的请求即使自己不限速（`interval_ms=0` 时不会走到这里），
    只要需要限速就得排队等锁，被批量建号的 400ms 节拍拖住
    （实测：interactive 自己没有前序请求，却被 batch 的 pacing 拖了 221ms）。

    预约式写法在并发下同样正确：锁内 `_pace_last` 单调递增，
    每个调用者拿到的 target 两两间隔 >= interval_ms。
    """
    if interval_ms <= 0:
        return
    interval = interval_ms / 1000.0
    async with _pace_lock:
        now = _now()
        target = max(now, _pace_last.get(lane, 0.0) + interval)
        _pace_last[lane] = target
    if target > now:
        await asyncio.sleep(target - now)


@asynccontextmanager
async def batch_lane():
    """
    把当前任务的 Emby 请求切到「批量 lane」（并发 1 + 最小间隔 + 熔断保护）。

    用法：
        async with batch_lane():
            await emby.emby_create(name, days)
    """
    token = _BATCH_LANE.set(True)
    try:
        yield
    finally:
        _BATCH_LANE.reset(token)


def enter_batch() -> None:
    """
    永久（对本任务/本 handler 而言）打开批量 lane —— 给「不好改缩进的大循环」用。

    典型场景：`bot/modules/commands/syncs.py` 的 `/restore_from_db`，
    只要在循环前 `register_throttle.enter_batch()`、循环里 `await register_throttle.tick()`
    两行就能接入限流，不必重构整段代码。

    ⚠️ 这里会**清掉上一次遗留的中止标记**（`_STOP`）。
    为什么：`request_stop()` 是一个全局 Event，一旦有人调用而没人配 `clear_stop()`，
    `tick()` 会在**之后每一次**批量任务的第一行就抛 `BatchAborted` ——
    表现为"批量命令一进去就中止"，而且极难排查。
    中止的语义应该是"打断**当前**这次批量任务"，不是"永久禁用批量功能"，
    所以在开新任务时复位。正在跑的批量任务不受影响（它们已经在 tick 循环里了）。
    """
    if is_stopping():
        _logger().warning(
            "[throttle] 检测到上一次遗留的批量中止标记，已在新任务开始时复位"
            "（中止只应打断当前任务，不应永久禁用批量功能）"
        )
        clear_stop()
    _BATCH_LANE.set(True)


def exit_batch() -> None:
    """关闭批量 lane（配合 enter_batch 使用）。"""
    _BATCH_LANE.set(False)


def in_batch_lane() -> bool:
    return bool(_BATCH_LANE.get())


# ──────────────────────────────────────────────────────────────────────────────
# 熔断器
# ──────────────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """连续失败熔断：连续 N 次超时/5xx/网络错误 → 打开 M 秒 → 冷却后放行探测。"""

    def __init__(self):
        self._fails = 0
        self._open_until = 0.0
        self._lock = asyncio.Lock()
        self._opened_times = 0
        self._alerted = False

    def is_open(self) -> bool:
        return _now() < self._open_until

    def remaining(self) -> float:
        return max(0.0, self._open_until - _now())

    @property
    def consecutive_failures(self) -> int:
        return self._fails

    @property
    def opened_times(self) -> int:
        return self._opened_times

    async def record(self, ok: bool, cfg: Dict[str, Any], detail: str = ""):
        async with self._lock:
            if ok:
                if self._fails:
                    _logger().info(f"[throttle] 熔断计数归零（上一次连续失败 {self._fails} 次）")
                self._fails = 0
                self._alerted = False
                return

            # 熔断进行中：**不再累积失败**。
            # 否则开闸期间 interactive 的快速探测失败会一路堆到阈值以上，
            # 冷却一结束就凭"一次失败"立刻再次开闸 —— breaker_failures 形同虚设。
            if self.is_open():
                return

            # 冷却刚刚到期（半开状态）：清零重新累积。
            # 判据是「`_open_until` 非零但已过期」—— 从未熔断过时它是 0.0，
            # 所以不会误清正常累积的计数（那样阈值就永远达不到了）。
            if self._open_until and not self.is_open():
                self._open_until = 0.0
                self._fails = 0

            self._fails += 1
            if self._fails >= int(cfg["breaker_failures"]):
                fails = self._fails
                self._open_until = _now() + float(cfg["breaker_cooldown"])
                self._opened_times += 1
                # 开闸即清零：下一次判定必须重新累积满 N 次失败，不能靠 1 次就再次开闸
                self._fails = 0
                # 每次开闸都允许告警。原实现只在成功时复位 `_alerted`，
                # 导致第二次熔断起 owner 全程静默。
                self._alerted = False
                text = (f"[throttle] ⛔ Emby 连续失败 {fails} 次，已熔断 "
                        f"{cfg['breaker_cooldown']:.0f}s（第 {self._opened_times} 次）。"
                        f"最近一次：{detail or '未知'}。批量建号已暂停，冷却后自动恢复。")
                _logger().error(text)
                if cfg.get("alert_owner") and not self._alerted:
                    self._alerted = True
                    asyncio.create_task(_alert_owner(text))

    async def wait_closed(self, cfg: Dict[str, Any], stop_event: Optional[asyncio.Event] = None,
                          interruptible: bool = True):
        """
        熔断期间排队等待。

        :param interruptible: True（默认）= 可被 stop_event / 全局 `request_stop()` 打断，
            返回 False；False = 只老老实实等冷却，不受中止标记影响。
            队列里的**用户注册**用 False：管理员中止一个批量任务（比如 /restore_from_db）
            不应该把正在排队的用户注册一起掐掉 —— 否则用户会看到"注册任务执行异常"，
            还会重复提交。
        """
        while self.is_open():
            wait = min(self.remaining(), 5.0)
            if not interruptible:
                await asyncio.sleep(wait)
                continue
            if await _sleep_or_stop(wait, stop_event):
                return False
        return True


_BREAKER = CircuitBreaker()

# 这些 error 属于「终态 4xx」，不算失败（不该触发熔断）
_TERMINAL_ERRORS = ("资源不存在", "认证失败，请检查API密钥", "权限不足")


def _probe_exempt(endpoint: str) -> bool:
    """
    熔断期「3 秒快速探测超时」**不适用**的端点（大响应体，套 3s 必然误杀）。

    判据是响应体量级，不是业务重要性：
      · 裸 `/emby/Sessions`（不带 ActiveWithinSeconds）：线上实测 2,970,545 B / 7.70s
      · 全量 `/emby/Users`、`/emby/Items` 列表：几千条记录，同样很大
    带 `ActiveWithinSeconds` 的 Sessions 只有 ~90 KB / 0.66s，属于小响应，照常探测。
    `/emby/Users/{id}` 是单用户，很小，不豁免。
    """
    if "ActiveWithinSeconds" in endpoint:
        return False
    path = endpoint.split("?")[0].rstrip("/")
    if path in ("/emby/Users", "/emby/Items"):
        return True
    return path.startswith("/emby/Sessions") or path.startswith("/emby/Items")


async def _alert_owner(text: str):
    try:
        from bot import bot as tg_bot, owner
        await tg_bot.send_message(owner, text)
    except Exception as e:  # pragma: no cover
        _logger().warning(f"[throttle] 熔断告警发送失败: {type(e).__name__}: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# 请求包装
# ──────────────────────────────────────────────────────────────────────────────

_ORIG_REQUEST: Optional[Callable] = None
# install() 会把 emby 单例的 max_retries 从 3 改成 2；这里记下原值供 uninstall() 还原
_ORIG_MAX_RETRIES: Optional[int] = None


async def _throttled_request(self, method: str, endpoint: str, timeout=None, **kwargs):
    """
    替换 `Embyservice._request`。保持完全相同的入参/返回（EmbyApiResult），
    只是在外面套上：并发闸门 → 最小间隔 → 熔断判断 → 原逻辑 → 结果记账。
    """
    cfg = config()
    if not cfg["enabled"]:
        return await _ORIG_REQUEST(self, method, endpoint, timeout, **kwargs)

    lane = "batch" if _BATCH_LANE.get() else "interactive"

    # 熔断期：批量直接快速失败（不占用 Emby 连接）；交互/巡检降级成快速探测，
    # 把原来的 3 次 × 10s 挂死压成 2 次 × probe_timeout，避免巡检把卡住的 Emby 继续拖住。
    if _BREAKER.is_open():
        if lane == "batch":
            return _fail_fast(endpoint, _BREAKER.remaining())
        # ⚠️ 只对「响应体很小」的请求套 3s 探测超时。大响应体（裸 /emby/Sessions
        # 线上实测 2.97 MB / 7.7s）套 3s 会必然超时，表现成"Emby 一熔断，巡检也跟着
        # 大面积报错"。默认配置走的是带 ActiveWithinSeconds 的小响应，踩不到；
        # 但只要有人把它配成 0 退回裸端点，这个地雷就会响 —— 所以这里显式豁免。
        if timeout is None and not _probe_exempt(endpoint):
            timeout = aiohttp.ClientTimeout(total=float(cfg["probe_timeout"]))

    if timeout is None and float(cfg["request_timeout"]) > 0:
        timeout = aiohttp.ClientTimeout(total=float(cfg["request_timeout"]))

    sem_batch, sem_interactive = await _semaphores(cfg)
    sem = sem_batch if lane == "batch" else sem_interactive
    interval = int(cfg["min_interval_ms"]) if lane == "batch" else int(cfg["interactive_min_interval_ms"])

    async with sem:
        await _pace(lane, interval)
        try:
            result = await _ORIG_REQUEST(self, method, endpoint, timeout, **kwargs)
        except Exception as e:
            await _BREAKER.record(False, cfg, f"{method} {endpoint} 异常 {type(e).__name__}: {e}")
            raise

    ok = bool(getattr(result, "success", False))
    if not ok:
        err = str(getattr(result, "error", "") or "")
        if any(t in err for t in _TERMINAL_ERRORS):
            ok = True  # 终态 4xx：不算"Emby 病了"
    await _BREAKER.record(ok, cfg, f"{method} {endpoint} -> {getattr(result, 'error', '')}")
    return result


def _fail_fast(endpoint: str, remaining: float):
    """构造一个"熔断中"的失败结果（不产生任何 HTTP 请求）。"""
    from bot.func_helper.emby import EmbyApiResult
    return EmbyApiResult(False, error=f"建号熔断中（{endpoint}），剩余冷却 {remaining:.0f}s")


# ──────────────────────────────────────────────────────────────────────────────
# 批量任务：分片 + 批间隔 + 可中断
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BatchReport:
    total: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    aborted: bool = False
    reason: str = ""
    elapsed: float = 0.0
    shards: int = 0
    failures: List[Any] = field(default_factory=list)

    def as_text(self) -> str:
        return (f"批量任务结束：共 {self.total} 项，成功 {self.ok}，失败 {self.failed}，"
                f"跳过 {self.skipped}，分片 {self.shards} 批，耗时 {self.elapsed:.1f}s"
                + (f"，已中止（{self.reason}）" if self.aborted else ""))


def request_stop(reason: str = "手动中止"):
    """请求中断所有批量任务（幂等；用 clear_stop() 复位）。"""
    _STOP.set()
    _logger().warning(f"[throttle] 收到批量任务中止请求：{reason}")


def clear_stop():
    _STOP.clear()


def is_stopping() -> bool:
    return _STOP.is_set()


async def _sleep_or_stop(seconds: float, stop_event: Optional[asyncio.Event] = None) -> bool:
    """可被中断的 sleep。返回 True 表示"被要求停止"。"""
    if seconds <= 0:
        return is_stopping() or (stop_event is not None and stop_event.is_set())
    stoppers = [asyncio.create_task(_STOP.wait())]
    if stop_event is not None:
        stoppers.append(asyncio.create_task(stop_event.wait()))
    try:
        done, pending = await asyncio.wait(stoppers, timeout=seconds,
                                           return_when=asyncio.FIRST_COMPLETED)
        return bool(done)
    finally:
        for t in stoppers:
            if not t.done():
                t.cancel()
        # 不用 await t：任务被取消时 await 会抛 CancelledError，
        # 在 finally 里吞掉它会掩盖外层真正的取消，改用 gather(return_exceptions=True)。
        await asyncio.gather(*stoppers, return_exceptions=True)


async def tick():
    """
    「批量循环」用的单行限速钩子：每调一次代表即将处理一个账号。

    行为：① 若熔断中则等待冷却（可被 request_stop 打断）；
         ② 检查中止标记；
         ③ 每处理满 shard_size 个，插入 batch_gap 秒的批间隔。

    ⚠️ 这里**刻意不再做 min_interval 节拍**。
    原实现在这里调了一次 `_pace("batch", min_interval_ms)`，而循环体里的
    `emby_create()` 又会为它的每个请求各调一次同样的 `_pace`，两边共用
    `_pace_last["batch"]` —— 于是每建 1 个号要占 **4** 个节拍（1 + 3）而不是 3 个。
    实测（3 个号 × 3 次请求，min_interval=100ms）：带 tick 1106ms / 不带 804ms。
    换算到线上 `min_interval_ms=400`：`/restore_from_db` 恢复 2013 个号
    每号 1.6s 而不是 1.2s，白多花约 13 分钟。

    去掉之后保护没有变弱：**Emby 侧的请求速率仍然被 `_throttled_request` 里的
    `_pace` 死死钉在 min_interval_ms**（这才是对 Emby 有意义的那个不变量），
    而账号之间的间隔自然等于"最后一个请求 → 下一个请求"的 400ms。
    """
    cfg = config()
    if not await _BREAKER.wait_closed(cfg):
        raise _BatchAborted("熔断等待期间收到中止请求")
    if is_stopping():
        raise _BatchAborted("收到中止请求")
    async with _tick_lock:
        now = _now()
        if now - _tick_state["last"] > float(cfg["batch_gap"]):
            _tick_state["count"] = 0  # 空闲超过一个批间隔，重新计数
        _tick_state["last"] = now
        _tick_state["count"] += 1
        n = _tick_state["count"]
    if n % int(cfg["shard_size"]) == 0:
        gap = float(cfg["batch_gap"])
        _logger().info(f"[throttle] 已完成 {n} 个，批间隔 {gap:.0f}s（可 request_stop() 打断）")
        if await _sleep_or_stop(gap):
            raise _BatchAborted("批间隔期间收到中止请求")


class _BatchAborted(Exception):
    """批量任务被主动/自动中止（熔断+abort_on_breaker、或 request_stop）。"""


# 公开别名：调用方（例如 syncs.py 的 /restore_from_db 循环）需要 catch 它
BatchAborted = _BatchAborted


async def run_batch(items: Sequence[Any],
                    fn: Callable[[Any], Awaitable[Any]],
                    *,
                    shard_size: Optional[int] = None,
                    batch_gap: Optional[float] = None,
                    stop_event: Optional[asyncio.Event] = None,
                    on_result: Optional[Callable[[Any, Any], None]] = None,
                    pace_items: bool = False,
                    name: str = "batch") -> BatchReport:
    """
    把一批任务按分片串行跑完，自带：批量 lane、并发闸门、最小间隔、批间隔、熔断暂停、可中断。

    :param items: 待处理项（每个元素 = 一个账号/一个用户对象）
    :param fn: 单条处理协程；抛异常视为该项失败，不影响后续项
    :param on_result: 可选回调 (item, result_or_exception)
    :param pace_items: 是否在**每个条目之间**再插一个 min_interval 节拍。
        默认 False —— 因为条目级节拍和 `_throttled_request` 里的请求级节拍
        共用 `_pace_last["batch"]`，如果 `fn` 自己会打 Emby（例如 `emby_create`
        一个号 3 次请求），两边叠加就变成每号 **4** 个节拍而不是 3 个，白白慢 1/3。
        只有当 `fn` **完全不打 Emby** 时，才需要传 True 来保证条目速率。
        （对 Emby 有意义的那个不变量始终是"请求速率"，由请求层保证。）
    """
    cfg = config()
    shard = max(1, int(shard_size or cfg["shard_size"]))
    gap = float(batch_gap if batch_gap is not None else cfg["batch_gap"])
    report = BatchReport(total=len(items))
    started = time.perf_counter()

    # 与 enter_batch() 同样的复位：中止只应打断"当前这一次"批量任务，
    # 不能因为上一次遗留的 _STOP 让新的批量任务一进来就 0 处理直接中止。
    if is_stopping():
        _logger().warning(
            f"[throttle] {name}: 检测到上一次遗留的批量中止标记，已在任务开始时复位"
        )
        clear_stop()

    token = _BATCH_LANE.set(True)
    try:
        for index, item in enumerate(items):
            if index and index % shard == 0:
                report.shards += 1
                _logger().info(f"[throttle] {name}: {index}/{len(items)} 完成，批间隔 {gap:.0f}s")
                if await _sleep_or_stop(gap, stop_event):
                    report.aborted, report.reason = True, "批间隔期间收到中止请求"
                    break
            if is_stopping() or (stop_event is not None and stop_event.is_set()):
                report.aborted, report.reason = True, "收到中止请求"
                break

            if _BREAKER.is_open():
                if cfg.get("abort_on_breaker"):
                    report.aborted = True
                    report.reason = "熔断中（abort_on_breaker=True）"
                    break
                if not await _BREAKER.wait_closed(cfg, stop_event):
                    report.aborted, report.reason = True, "熔断等待期间收到中止请求"
                    break

            attempts = 1 + max(0, int(cfg["item_retries"]))
            result = None
            error: Any = None
            aborted = False
            for attempt in range(attempts):
                if pace_items:
                    await _pace("batch", int(cfg["min_interval_ms"]))
                try:
                    result = await fn(item)
                except _BatchAborted as e:
                    report.aborted, report.reason = True, str(e)
                    aborted = True
                    break
                except Exception as e:
                    error = e
                    result = None
                else:
                    error = None
                    if result is not False and result is not None:
                        break
                    error = RuntimeError("处理函数返回失败")

                if attempt + 1 < attempts:
                    # 指数退避 + 抖动（可被 request_stop 打断）
                    delay = min(float(cfg["backoff_cap"]),
                                float(cfg["backoff_base"]) * (2 ** attempt)) + random.uniform(0, 0.3)
                    _logger().warning(
                        f"[throttle] {name} 第 {index + 1} 项失败，{delay:.1f}s 后第 {attempt + 2}/{attempts} 次尝试：{error}")
                    if await _sleep_or_stop(delay, stop_event):
                        report.aborted, report.reason = True, "退避等待期间收到中止请求"
                        aborted = True
                        break
            if aborted:
                break

            if result is False or result is None:
                report.failed += 1
                report.failures.append((item, error))
                _logger().error(f"[throttle] {name} 第 {index + 1} 项最终失败: {error}")
                if on_result:
                    on_result(item, error)
                continue

            report.ok += 1
            if on_result:
                on_result(item, result)
    finally:
        _BATCH_LANE.reset(token)
        report.elapsed = time.perf_counter() - started
        _logger().info(f"[throttle] {report.as_text()}")
    return report


# ──────────────────────────────────────────────────────────────────────────────
# 最小请求数的建号快路径（可选启用）
# ──────────────────────────────────────────────────────────────────────────────

_vf_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_vf_lock = asyncio.Lock()


async def cached_virtual_folders(ttl: Optional[float] = None, force: bool = False) -> Dict[str, str]:
    """
    带 TTL 的 `GET /emby/Library/VirtualFolders` 缓存（单飞，避免缓存击穿）。

    原实现**每个账号**都要拉一次媒体库列表（emby.py:486），批量建号时这部分
    99% 是重复流量。
    """
    from bot.func_helper.emby import emby

    cfg = config()
    ttl = float(cfg["virtualfolders_ttl"] if ttl is None else ttl)
    now = _now()
    if not force and _vf_cache["data"] is not None and now - _vf_cache["at"] < ttl:
        return dict(_vf_cache["data"])

    async with _vf_lock:
        now = _now()
        if not force and _vf_cache["data"] is not None and now - _vf_cache["at"] < ttl:
            return dict(_vf_cache["data"])
        result = await emby._request("GET", "/emby/Library/VirtualFolders")
        if not result.success or not isinstance(result.data, list):
            _logger().warning(f"[throttle] 拉取媒体库列表失败：{getattr(result, 'error', '')}")
            return dict(_vf_cache["data"] or {})
        data = {lib["Guid"]: lib["Name"] for lib in result.data if lib.get("Guid")}
        _vf_cache["data"] = data
        _vf_cache["at"] = _now()
        return dict(data)


def invalidate_virtual_folders():
    _vf_cache["data"] = None
    _vf_cache["at"] = 0.0


async def emby_create_safe(name: str, days: int):
    """
    **已改为 `emby.emby_create()` 的薄封装，不再自己实现一份。**

    为什么改：这里原本复制了一份"3 次 HTTP 快路径"的策略计算，与
    `bot/func_helper/emby.py::Embyservice.emby_create()` 里的快路径是两份会漂移的实现，
    而且它复刻的是**错误**的等价性 —— 无条件写 `EnableAllFolders=False` +
    `EnabledFolders=全部库−被屏蔽库`，而原实现只有在 `emby_block + extra_emby_libs`
    的名字能在 Emby 里对上时才会这么写；对不上时（**线上正是这种情况**）
    `hide_folders_by_names()` 提前 return，`EnableAllFolders` 保持 Emby 默认的 `true`。
    照抄错误分支会让"以后新加的媒体库对这些用户不可见"。

    现在唯一的实现只有一处（`Embyservice.emby_create`），这里只做转发，
    请求数、返回值、孤儿账号回滚语义全部以它为准。
    """
    from bot.func_helper.emby import emby as _emby

    return await _emby.emby_create(name=name, days=days)


# ──────────────────────────────────────────────────────────────────────────────
# 安装 / 卸载 / 状态
# ──────────────────────────────────────────────────────────────────────────────

_INSTALLED = False
_ORIG_WORKER_COUNT: Optional[Callable] = None
_ORIG_PROCESS_JOB: Optional[Callable] = None


async def _throttled_process_job(self, job):
    """
    包装 `RegisterQueueManager._process_job`：队列 worker 处理每个 job 时
    ① 切到批量 lane；② 熔断时先等冷却；③ 保证 worker 之间的最小间隔。

    刻意**不改** `_worker_loop` 的任何记账逻辑（_active_jobs/_reserved_slots/task_done），
    避免引入队列计数错乱。

    ⚠️ 这里**不会**抛 `_BatchAborted`（原实现会在收到 `request_stop()` 时抛）：
    队列里跑的是**用户自己发起的注册**，不该被管理员的批量中止波及 ——
    而且 `_worker_loop` 用的是 `except Exception`，抛出去会变成用户看到的
    "注册任务执行异常，请稍后重试"，用户还会以为失败而重复提交。
    所以这里等熔断冷却时用 `interruptible=False`，只等不中断。
    """
    cfg = config()
    token = _BATCH_LANE.set(True)
    try:
        await _BREAKER.wait_closed(cfg, interruptible=False)
        await _pace("batch", int(cfg["min_interval_ms"]))
        return await _ORIG_PROCESS_JOB(self, job)
    finally:
        _BATCH_LANE.reset(token)


def _capped_worker_count(self) -> int:
    """限制 `register_worker_count` 的上限（只防增长；缩容请改 config 后重启）。"""
    n = _ORIG_WORKER_COUNT(self)
    cap = int(config()["max_workers"])
    return max(1, min(int(n), cap))


def install() -> bool:
    """
    挂载限流（幂等）。返回 True 表示本次真的挂了。

    挂载点：
      · `Embyservice._request`                     —— 全局请求闸门 + 熔断 + 快速探测
      · `Embyservice.max_retries`                  —— 幂等请求重试次数（默认 3 → 2）
      · `RegisterQueueManager._process_job`        —— 队列任务走批量 lane
      · `RegisterQueueManager._configured_worker_count` —— worker 数上限
    """
    global _INSTALLED, _ORIG_REQUEST, _ORIG_WORKER_COUNT, _ORIG_PROCESS_JOB, _ORIG_MAX_RETRIES

    cfg = config()
    log = _logger()
    if _INSTALLED:
        log.info("[throttle] install() 重复调用，已忽略")
        return False

    try:
        from bot.func_helper.emby import Embyservice, emby as _emby
        from bot.func_helper.register_queue import RegisterQueueManager
    except Exception as e:
        log.error(f"[throttle] 挂载失败（导入异常）: {type(e).__name__}: {e}")
        return False

    if not cfg["enabled"]:
        log.warning("[throttle] enabled=false，限流闸门未挂载（保持原行为）")
        return False

    _ORIG_REQUEST = Embyservice._request
    Embyservice._request = _throttled_request
    # 幂等请求（GET 等）的重试次数：3 → 2，把单个 GET 最坏 33s 压到 21s
    try:
        _ORIG_MAX_RETRIES = int(getattr(_emby, "max_retries", 3))
        _emby.max_retries = max(1, int(cfg["retries"]))
    except Exception:
        _ORIG_MAX_RETRIES = None

    _ORIG_PROCESS_JOB = RegisterQueueManager._process_job
    RegisterQueueManager._process_job = _throttled_process_job
    _ORIG_WORKER_COUNT = RegisterQueueManager._configured_worker_count
    RegisterQueueManager._configured_worker_count = _capped_worker_count

    _INSTALLED = True
    # 注意：bot.LOGGER 是 loguru，占位符是 {}，不能用 %s 参数化写法（会原样打印 %s）
    log.warning(
        f"[throttle] 安全批量建号限流已挂载：批量并发={cfg['batch_concurrency']}，"
        f"交互并发={cfg['interactive_concurrency']}，worker上限={cfg['max_workers']}，"
        f"最小间隔={cfg['min_interval_ms']}ms，分片={cfg['shard_size']}/批，"
        f"批间隔={cfg['batch_gap']}s，熔断={cfg['breaker_failures']}次/{cfg['breaker_cooldown']}s，"
        f"请求超时={cfg['request_timeout']}s，重试={cfg['retries']}，"
        f"VirtualFolders缓存={cfg['virtualfolders_ttl']}s，配置来源={_config_source()}"
    )
    return True


def _config_source() -> str:
    if os.environ.get(ENV_PREFIX + "ENABLED") is not None:
        return "环境变量"
    if os.path.exists(SIDECAR_NAME):
        return f"侧车文件 {SIDECAR_NAME}"
    return "默认值 / _open"


def uninstall() -> bool:
    """还原到原始行为（用于排障对比）。"""
    global _INSTALLED
    if not _INSTALLED:
        return False
    from bot.func_helper.emby import Embyservice, emby as _emby
    from bot.func_helper.register_queue import RegisterQueueManager
    Embyservice._request = _ORIG_REQUEST
    RegisterQueueManager._process_job = _ORIG_PROCESS_JOB
    RegisterQueueManager._configured_worker_count = _ORIG_WORKER_COUNT
    if _ORIG_MAX_RETRIES is not None:
        try:
            _emby.max_retries = _ORIG_MAX_RETRIES
        except Exception:
            pass
    _INSTALLED = False
    _logger().warning("[throttle] 限流已卸载，恢复原始行为")
    return True


def status() -> Dict[str, Any]:
    """当前运行状态（可直接喂给管理面板/日志）。"""
    cfg = config()
    return {
        "installed": _INSTALLED,
        "enabled": cfg["enabled"],
        "config_source": _config_source(),
        "breaker_open": _BREAKER.is_open(),
        "breaker_remaining_s": round(_BREAKER.remaining(), 1),
        "consecutive_failures": _BREAKER.consecutive_failures,
        "breaker_opened_times": _BREAKER.opened_times,
        "stopping": is_stopping(),
        "batch_in_flight": _gate_batch.in_flight,
        "batch_capacity": _gate_batch.capacity,
        "interactive_in_flight": _gate_interactive.in_flight,
        "interactive_capacity": _gate_interactive.capacity,
        "config": cfg,
    }
