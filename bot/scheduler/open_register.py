# -*- coding: utf-8 -*-
"""
定时开注任务（一次性 · 3 天）—— 2026-10-14 创建

需求：
  2026-10-14 / 10-15 / 10-16 每晚 21:00（北京时间）开放注册
    · 每次新增 100 个名额（all_user = 当前已注册人数 + 100）
    · 开放 30 分钟，21:30 自动关闭
    · 开注、关注 各在群内发一条通知

实现说明：
  - 本文件挂载到容器 /app/bot/scheduler/open_register.py
  - 由 scheduler/__init__.py（挂载自 patch/watching/scheduler_init.py）导入；
    模块级 add_job 进入 APScheduler pending 队列，main.py 启动钩子 scheduler.start() 后生效
  - 开/关 用两个独立 cron 时点，容器中途重启也不会丢掉“关闭”动作
  - 本任务只动 _open 的 all_user / stat / timing，不碰其它配置
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from bot import bot, group, _open, save_config, LOGGER
from bot.func_helper.scheduler import scheduler

TZ = "Asia/Shanghai"
YEAR, MONTH, DAYS = 2026, 10, "14,15,16"     # 10/14、10/15、10/16
OPEN_HOUR, OPEN_MINUTE = 21, 0               # 每晚 21:00 开注
DURATION_MIN = 30                            # 开放时长（分钟）
CLOSE_MINUTE = OPEN_MINUTE + DURATION_MIN    # 21:30 自动关闭
QUOTA = 100                                  # 每次新增名额
GROUP_ID = group[0]

# 开注时记录基准人数，供关注时统计“本次新增”；进程重启则退化为不显示该项
_baseline = {"tem": None}


async def open_register():
    """开注：新增 QUOTA 个名额并开放注册"""
    try:
        before = _open.tem
        _baseline["tem"] = before
        _open.all_user = before + QUOTA
        _open.timing = DURATION_MIN
        _open.stat = True
        save_config()
        LOGGER.info(
            f"【定时开注】已开放注册：已注册 {before} 人 → 总限额 {_open.all_user}"
            f"（新增 {QUOTA}），{DURATION_MIN} 分钟后自动关闭"
        )
        text = (
            f"**🎉 ChaPanda｜开放注册**\n"
            f"⏰ 本次名额 **{QUOTA}** 人，现已开放注册\n"
            f"📝 名额有限，先到先得，满员即止\n"
            f"🕘 本次开放 **{DURATION_MIN} 分钟**，到时自动关闭"
        )
        await bot.send_message(GROUP_ID, text)
    except Exception as e:
        LOGGER.error(f"【定时开注】开注失败：{e}")


async def close_register():
    """关注：关闭注册并汇报本次新增"""
    try:
        if not _open.stat:
            LOGGER.info("【定时开注】关闭动作跳过：注册当前本就未开放")
            return
        _open.stat = False
        _open.timing = 0
        save_config()
        before = _baseline.get("tem")
        added = (_open.tem - before) if isinstance(before, int) else None
        line = f"🆕 本次新增：**{added}** 人\n" if (added is not None and added >= 0) else ""
        LOGGER.info(f"【定时开注】注册已关闭：累计 {_open.tem} / 总限额 {_open.all_user}")
        text = (
            f"**🔒 ChaPanda｜注册已关闭**\n"
            f"⏰ 本次 {DURATION_MIN} 分钟开放已结束\n"
            f"{line}"
            f"📊 当前注册：**{_open.tem}** / {_open.all_user}"
        )
        await bot.send_message(GROUP_ID, text)
    except Exception as e:
        LOGGER.error(f"【定时开注】关注失败：{e}")


# ---- 注册定时点 ----
_open_trig = CronTrigger(year=YEAR, month=MONTH, day=DAYS,
                         hour=OPEN_HOUR, minute=OPEN_MINUTE, timezone=TZ)
_close_trig = CronTrigger(year=YEAR, month=MONTH, day=DAYS,
                          hour=OPEN_HOUR, minute=CLOSE_MINUTE, timezone=TZ)

# ---- 2026-10-15 用户要求取消 10-15 与 10-16 两晚开注：任务已停用 ----
ENABLED = False
if ENABLED:
    scheduler.add_job(open_register, _open_trig,
                      id="open_register_1014", replace_existing=True, misfire_grace_time=1800)
    scheduler.add_job(close_register, _close_trig,
                      id="close_register_1014", replace_existing=True, misfire_grace_time=300)

_now = datetime.now(ZoneInfo(TZ))
if ENABLED:
    LOGGER.info(
        f"【定时开注】任务已装载 | 现在 {_now:%Y-%m-%d %H:%M:%S} | "
        f"下次开注 {_open_trig.get_next_fire_time(None, _now)} | "
        f"下次关注 {_close_trig.get_next_fire_time(None, _now)}"
    )
else:
    LOGGER.info("【定时开注】任务已停用：不再于 10-15 / 10-16 开注")
