import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from bot import LOGGER, _open, emby_line, config, schedall
from bot.func_helper.concurrency import get_user_lock
from bot.func_helper.emby import emby
from bot.func_helper.fix_bottons import re_create_ikb
from bot.func_helper.msg_utils import editMessage, sendMessage
from bot.func_helper.utils import tem_adduser
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby

# ── 排队体验参数 ────────────────────────────────────────────────────────────
# 全部通过 getattr(_open, ...) 读取：schemas 里没有这些字段时用默认值，不会报错。
ETA_WINDOW_DEFAULT = 10          # 滑动平均窗口：最近 N 个已完成 job
ETA_MIN_SAMPLES_DEFAULT = 3      # 少于该样本数时只给粗略区间
WARN_AFTER_SECONDS_DEFAULT = 120  # 等待超过该秒数补一条"仍在排队"提示
WARN_REPEAT_MAX = 3               # 同一用户最多补几条排队提示（防止刷 Telegram 编辑配额）


def _eta_window() -> int:
    return max(1, int(getattr(_open, "register_queue_eta_window", ETA_WINDOW_DEFAULT) or ETA_WINDOW_DEFAULT))


def _eta_min_samples() -> int:
    return max(1, int(getattr(_open, "register_queue_eta_min_samples", ETA_MIN_SAMPLES_DEFAULT) or ETA_MIN_SAMPLES_DEFAULT))


def _warn_after_seconds() -> int:
    return max(0, int(getattr(_open, "register_queue_warn_after_seconds", WARN_AFTER_SECONDS_DEFAULT) or 0))


def format_duration(seconds: float) -> str:
    """秒数 → 用户能读的中文时长（不暴露内部实现）。"""
    total = max(0, int(round(float(seconds))))
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if sec == 0:
        return f"{minutes} 分钟"
    return f"{minutes} 分 {sec} 秒"


def format_eta(eta_seconds: Optional[float], samples: int) -> str:
    """
    把 ETA 秒数变成文案。

    样本充足 → 仍给区间（±20%/+40%），因为建号耗时抖动很大；
    样本不足 → 给更宽的区间并注明"约"；
    完全没有样本 → 只说在估算，不装精确。
    """
    if eta_seconds is None or int(samples) <= 0:
        return "预计耗时正在估算中"
    value = max(0.0, float(eta_seconds))
    if int(samples) < _eta_min_samples():
        return f'预计约 {format_duration(value * 0.5)} ~ {format_duration(value * 1.8)}'
    return f'预计约 {format_duration(value * 0.8)} ~ {format_duration(value * 1.4)}'


@dataclass
class RegisterJob:
    user_id: int
    username: str
    pwd2: str
    stats: bool
    days: int
    status_message: object
    # 观测用字段（新增，带默认值，不影响既有位置参数构造）：
    #   outcome: None=尚未处理完, "ok"=成功建号, "failed"=失败（失败也要进耗时样本）
    #   started: worker 是否已经开始处理该 job
    outcome: Optional[str] = None
    started: bool = False


def slot_full_message(reserved: int = 0) -> str:
    """
    生成"注册席位已满"的提示文案。

    注意字段语义（易错点）：
      _open.tem      = **已注册人数**，不是剩余。见 utils.tem_adduser()：有人
                       注册成功就 +1，达到 all_user 时把 stat 置 False。
      _open.all_user = 总注册限制。
      剩余席位 = all_user - tem，必须相减才算。

    早期版本把 tem 直接标成"剩余可注册总数"，于是 tem=878 / all_user=882 时
    会显示成"剩余可注册总数(878)，已达总注册限制(882)" —— 数字看着自相矛盾
    （用户会以为"还剩 878 却说满了"），实际是 878 个已注册、真实剩余 4 席。

    :param reserved: 已占位但尚未处理完的注册数（enqueue 判定把它算进上限了，
                     文案必须一并交代，否则用户不理解"明明还剩几个席位却提示已满"）
    """
    limit = int(_open.all_user or 0)
    used = int(_open.tem or 0)
    remaining = max(0, limit - used)

    text = (
        f'**🚫 很抱歉，注册席位已满。**\n\n'
        f'· 已注册 | **{used}**\n'
        f'· 总注册限制 | **{limit}**\n'
        f'· 剩余席位 | **{remaining}**'
    )
    if reserved > 0:
        text += f'\n\n__其中 {reserved} 个席位已被排队中的注册占位，请稍后再试。__'
    text += '\n\n__注意：这次是**席位**不够，不是排队的人多 —— 等有席位空出来后即可注册。__'
    return text


def queue_full_message(waiting_limit: int = 0) -> str:
    """
    "排队满了"文案 —— 与 slot_full_message 严格区分。

    席位满 = tem + reserved >= all_user（没名额了）；
    排队满 = 等待队列达到本次可用上限（还有名额，只是等位的人挤满了）。
    两种情况的用户动作完全不同，不能糊成一条文案。
    """
    text = (
        '**⏳ 排队的人暂时太多了**\n\n'
        '· 注册席位还有空余 —— 只是等位的人数满了\n'
    )
    if int(waiting_limit or 0) > 0:
        text += f'· 当前可排队人数 | **{int(waiting_limit)}** 人\n'
    text += '\n__请稍等 1~2 分钟再点一次「创建账户」，队伍会陆续空出来。__'
    return text


class RegisterQueueManager:
    def __init__(self):
        self._queue: asyncio.Queue[RegisterJob] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._busy_users: set[int] = set()
        self._reserved_slots = 0
        self._lock = asyncio.Lock()
        self._active_jobs = 0
        # ── 以下字段只用于观测/文案，不参与容量与记账判定 ──
        self._waiting_order: list[int] = []            # 仍在排队（未被 worker 取走）的用户，FIFO
        self._active_users: set[int] = set()           # 正在被处理中的用户
        self._duration_samples: list[tuple[float, bool]] = []  # (wall-clock 秒, 是否成功)
        self._warn_tasks: dict[int, asyncio.Task] = {}  # user_id -> 超时提醒任务

    def _configured_worker_count(self) -> int:
        return max(1, int(getattr(_open, "register_worker_count", 5) or 5))

    def _configured_queue_limit(self) -> int:
        return max(1, int(getattr(_open, "register_queue_limit", 100) or 100))

    def _remaining_slot_count_locked(self) -> int:
        return max(0, int(_open.all_user) - int(_open.tem or 0))

    def _max_waiting_queue_size_locked(self) -> int:
        remaining_after_active = self._remaining_slot_count_locked() - self._active_jobs
        return max(0, min(self._configured_queue_limit(), remaining_after_active))

    async def ensure_started(self):
        async with self._lock:
            self._workers = [task for task in self._workers if not task.done()]
            missing = self._configured_worker_count() - len(self._workers)
            for index in range(missing):
                task = asyncio.create_task(self._worker_loop(index), name=f"register-worker-{index}")
                self._workers.append(task)

    async def is_user_busy(self, user_id: int) -> bool:
        async with self._lock:
            return user_id in self._busy_users

    def reserved_slot_count(self) -> int:
        """
        已占位（排队中或处理中）但尚未计入 _open.tem 的席位数。

        仅供展示用，不加锁：读到的值最多略有滞后，不影响正确性。
        """
        return int(self._reserved_slots)

    # ── 只读观测接口 ────────────────────────────────────────────────────────
    def waiting_queue_limit(self) -> int:
        """当前允许排队的人数上限（席位不足时会同步缩小，保证不超卖）。"""
        return int(self._max_waiting_queue_size_locked())

    def remaining_slot_count(self) -> int:
        """还剩多少未占用的席位 = all_user - tem - reserved。"""
        return max(0, self._remaining_slot_count_locked() - int(self._reserved_slots))

    def _effective_worker_count(self) -> int:
        """
        生效的并发数：优先用真正在跑的 worker 数，未启动时退回配置值
        （throttle 补丁会在类层面把配置值压到 max_workers，这里直接沿用它的结果）。
        """
        alive = len([task for task in self._workers if not task.done()])
        if alive > 0:
            return max(1, alive)
        return max(1, int(self._configured_worker_count()))

    def _trim_samples(self) -> list[tuple[float, bool]]:
        window = _eta_window()
        if len(self._duration_samples) > window:
            self._duration_samples = self._duration_samples[-window:]
        return self._duration_samples

    def _eta_inputs(self) -> tuple[Optional[float], int]:
        """返回 (窗口内平均耗时秒, 样本数)。没有样本时平均值为 None。"""
        samples = self._trim_samples()
        if not samples:
            return None, 0
        return sum(item[0] for item in samples) / len(samples), len(samples)

    def eta_for(self, ahead: int) -> Optional[float]:
        """
        预计还要等多少秒。

        算法：窗口内平均 wall-clock 耗时 ÷ 生效并发数 × 前面的人数。
        耗时用 time.monotonic() 在 worker 里实测（含 Emby 调用与限流间隔），
        失败 job 也计入样本（失败同样占用了一个 worker 的时间片）。
        说明：ahead 含"正在处理中"的 job，所以估算偏保守。
        """
        average, samples = self._eta_inputs()
        if average is None or samples <= 0:
            return None
        pending = max(0, int(ahead))
        return max(0.0, float(average)) * pending / self._effective_worker_count()

    def waiting_line(self, ahead: int, waiting: bool = False) -> str:
        """
        生成面向用户的一行排队状态："你前面还有 N 位 · 预计约 M 秒"。

        :param waiting: True 表示这是"已经等了一段时间"的补提示 —— 此时即使
                        前面没人也不能说"马上开始"（并发槽位可能还没空出来）。
        """
        pending = max(0, int(ahead))
        if pending == 0:
            if waiting:
                return '你排在队首，正在等前面几位收尾，马上轮到你。'
            return '🎉 你排在队首，马上开始创建账号。'
        _, samples = self._eta_inputs()
        return f'你前面还有 **{pending}** 位 · {format_eta(self.eta_for(pending), samples)}'

    def stats(self) -> dict:
        """
        只读快照，供面板 / 状态命令展示。不修改任何计数。

        返回：waiting / active / workers / reserved / avg_seconds / samples /
              failures / remaining_slots / queue_limit / waiting_limit / eta_for(n)
        """
        average, samples = self._eta_inputs()
        waiting = int(self._queue.qsize())
        return {
            "waiting": waiting,
            "active": int(self._active_jobs),
            "workers": self._effective_worker_count(),
            "reserved": int(self._reserved_slots),
            "avg_seconds": None if average is None else round(float(average), 3),
            "samples": samples,
            "failures": sum(1 for _duration, ok in self._duration_samples if not ok),
            "remaining_slots": self.remaining_slot_count(),
            "queue_limit": self._configured_queue_limit(),
            "waiting_limit": self.waiting_queue_limit(),
            "eta_for": self.eta_for,
        }

    def user_queue_position(self, user_id: int) -> Optional[int]:
        """
        用户在队列中的位置（1 = 下一个被处理）。

        返回 None = 不在队列里；0 = 正在创建中。
        只读，不加锁；读到的值最多略有滞后。
        """
        if user_id in self._active_users:
            return 0
        try:
            index = self._waiting_order.index(user_id)
        except ValueError:
            return None
        return int(self._active_jobs) + index + 1

    async def enqueue(self, job: RegisterJob) -> tuple[bool, str, Optional[int]]:
        await self.ensure_started()
        async with self._lock:
            if job.user_id in self._busy_users:
                # 已在队列里：回位置而不是再排一个（去重）
                return False, "duplicate", self.user_queue_position(job.user_id)
            current_tem = int(_open.tem or 0)
            if current_tem + self._reserved_slots >= _open.all_user:
                return False, "slot_full", None
            if self._queue.qsize() >= self._max_waiting_queue_size_locked():
                return False, "queue_full", None

            ahead = self._active_jobs + self._queue.qsize()
            self._busy_users.add(job.user_id)
            self._reserved_slots += 1
            await self._queue.put(job)
            self._waiting_order.append(job.user_id)
            self._schedule_wait_warning(job)
            return True, "queued", ahead + 1

    # ── 观测记账（不改 _active_jobs / _reserved_slots / _busy_users / task_done 语义）──
    def _on_job_started(self, job: RegisterJob):
        """worker 取到 job：标记已开始，并把它从"排队中"挪到"处理中"。"""
        job.started = True
        self._cancel_wait_warning(job.user_id)
        try:
            self._waiting_order.remove(job.user_id)
        except ValueError:
            pass
        self._active_users.add(job.user_id)

    def _on_job_finished(self, job: RegisterJob):
        self._active_users.discard(job.user_id)
        self._cancel_wait_warning(job.user_id)

    def _record_job_duration(self, job: RegisterJob, seconds: float):
        """记录一个已完成 job 的真实 wall-clock 耗时（失败也计入，但打上标记）。"""
        self._duration_samples.append((max(0.0, float(seconds)), job.outcome == "ok"))
        self._trim_samples()

    def _schedule_wait_warning(self, job: RegisterJob):
        """等待过久时补"仍在排队"提示（间隔可配，默认 120s，最多 WARN_REPEAT_MAX 次）。"""
        delay = _warn_after_seconds()
        if delay <= 0:
            return
        try:
            task = asyncio.create_task(
                self._wait_warning(job, delay), name=f"register-queue-warn-{job.user_id}"
            )
        except RuntimeError:  # 没有运行中的事件循环时直接跳过
            return
        self._warn_tasks[job.user_id] = task

    async def _wait_warning(self, job: RegisterJob, delay: int):
        """
        等待过久就给用户刷新一次排队状态（位置 + ETA）。

        队列拉长后尾部可能等十几分钟，只提示一次太干；这里以
        `register_queue_warn_after_seconds` 为间隔重复，但最多 WARN_REPEAT_MAX 次，
        避免长时间占着 Telegram 的编辑配额。job 一开始处理就取消
        （见 _on_job_started / _cancel_wait_warning）。
        """
        try:
            for _ in range(WARN_REPEAT_MAX):
                await asyncio.sleep(delay)
                if job.started or job.outcome is not None:
                    return
                position = self.user_queue_position(job.user_id)
                if position is None or position <= 0:
                    return
                await self._safe_edit(
                    job.status_message,
                    f'⏳ **仍在排队中，请再稍等一会儿**\n\n'
                    f'{self.waiting_line(position - 1, waiting=True)}\n\n'
                    f'__创建完成后我会在这里直接通知你，请勿重复提交。__',
                )
        except asyncio.CancelledError:
            return
        except Exception as e:
            LOGGER.warning(f"注册队列排队提示发送失败: tg={job.user_id}, error={e}")

    def _cancel_wait_warning(self, user_id: int):
        task = self._warn_tasks.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _worker_loop(self, worker_index: int):
        while True:
            job = await self._queue.get()
            async with self._lock:
                self._active_jobs += 1
            # 以下均为只读观测：开始时刻 / 排队→处理中的状态迁移
            self._on_job_started(job)
            started_at = time.monotonic()

            try:
                await self._process_job(job)
            except Exception as e:
                LOGGER.exception(f"注册队列worker异常[{worker_index}]: {e}")
                await self._safe_edit(job.status_message, "❌ 注册任务执行异常，请稍后重试。", re_create_ikb)
            finally:
                self._record_job_duration(job, time.monotonic() - started_at)
                async with self._lock:
                    self._active_jobs = max(0, self._active_jobs - 1)
                    self._busy_users.discard(job.user_id)
                    self._reserved_slots = max(0, self._reserved_slots - 1)
                self._on_job_finished(job)
                self._queue.task_done()

    async def _process_job(self, job: RegisterJob):
        job.outcome = "failed"
        async with get_user_lock(job.user_id):
            current = sql_get_emby(tg=job.user_id)
            if not current:
                return await self._safe_edit(job.status_message, "⚠️ 数据库没有你，请重新 /start录入")
            if current.embyid:
                return await self._safe_edit(job.status_message, "💦 你已经有账户啦！请勿重复注册。")
            if not job.stats and int(current.us or 0) <= 0:
                return await self._safe_edit(job.status_message, "🤖 当前没有可用注册资格，请重新领取注册码后再试。")
            if _open.tem >= _open.all_user:
                # 此处席位已被真实消耗，remaining 必为 0，无需再提占位数
                return await self._safe_edit(job.status_message, slot_full_message())

            await self._safe_edit(
                job.status_message,
                f'🆗 已进入处理\n\n用户名：**{job.username}**  安全码：**{job.pwd2}** \n\n'
                f'__正在创建账号…（这一步要等 emby 建号，请勿关闭会话）__......',
            )

            data = await emby.emby_create(name=job.username, days=job.days)
            if not data:
                return await self._safe_edit(
                    job.status_message,
                    '**- ❎ 已有此账户名，请重新输入注册\n- ❎ 或检查有无特殊字符\n- ❎ 或emby服务器连接不通，会话已结束！**',
                    re_create_ikb,
                )

            pwd = data[1]
            eid = data[0]
            ex = data[2]

            refreshed = sql_get_emby(tg=job.user_id)
            if not refreshed or refreshed.embyid:
                await self._rollback_created_account(job.user_id, eid, "创建后检测到账户状态已变化")
                return await self._safe_edit(job.status_message, '⚠️ 账户状态已变化，请重新打开面板确认。')

            if job.stats:
                updated = sql_update_emby(
                    Emby.tg == job.user_id,
                    embyid=eid,
                    name=job.username,
                    pwd=pwd,
                    pwd2=job.pwd2,
                    lv='b',
                    cr=datetime.now(),
                    ex=ex,
                )
            else:
                updated = sql_update_emby(
                    Emby.tg == job.user_id,
                    embyid=eid,
                    name=job.username,
                    pwd=pwd,
                    pwd2=job.pwd2,
                    lv='b',
                    cr=datetime.now(),
                    ex=ex,
                    us=0,
                )

            if not updated:
                await self._rollback_created_account(job.user_id, eid, "创建后写入数据库失败")
                return await self._safe_edit(job.status_message, "❌ 账户初始化失败，请稍后重试。")

            tem_adduser()

            if schedall.check_ex:
                ex_text = ex.strftime("%Y-%m-%d %H:%M:%S")
            elif schedall.low_activity:
                ex_text = f'__若{config.activity_check_days}天无观看将封禁__'
            else:
                ex_text = '__无需保号，放心食用__'

            job.outcome = "ok"
            await self._safe_edit(
                job.status_message,
                f'**▎创建用户成功🎉**\n\n'
                f'· 用户名称 | `{job.username}`\n'
                f'· 用户密码 | `{pwd}`\n'
                f'· 安全密码 | `{job.pwd2}`（仅发送一次）\n'
                f'· 到期时间 | `{ex_text}`\n'
                f'· 当前线路：\n'
                f'{emby_line}\n\n'
                f'**·【服务器】 - 查看线路和密码**',
            )

    async def _safe_edit(self, message, text: str, buttons=None):
        result = await editMessage(message, text, buttons)
        if result is True:
            return True
        return await sendMessage(message, text, buttons=buttons)

    async def _rollback_created_account(self, user_id: int, emby_id: str, reason: str):
        LOGGER.warning(f"注册队列回滚远端账户: tg={user_id}, emby_id={emby_id}, reason={reason}")
        try:
            deleted = await emby.emby_del(emby_id=emby_id)
            if not deleted:
                LOGGER.error(f"注册队列回滚失败: tg={user_id}, emby_id={emby_id}")
        except Exception as e:
            LOGGER.exception(f"注册队列回滚异常: tg={user_id}, emby_id={emby_id}, error={e}")


_register_queue_manager: Optional[RegisterQueueManager] = None


def get_register_queue_manager() -> RegisterQueueManager:
    global _register_queue_manager
    if _register_queue_manager is None:
        _register_queue_manager = RegisterQueueManager()
    return _register_queue_manager
