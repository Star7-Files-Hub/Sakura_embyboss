#! /usr/bin/python3
# -*- coding: utf-8 -*-

import asyncio
from bot import bot, LOGGER

# 安全批量建号限流闸门（可选模块）：把 Embyservice._request 与注册队列 worker
# 挂到统一闸门上，避免批量建号把 Emby 打挂。import/挂载失败都不影响启动。
try:
    import bot.func_helper.register_throttle as register_throttle
    register_throttle.install()
except Exception as _throttle_err:  # pragma: no cover
    LOGGER.error(f"挂载安全批量建号限流失败（将按原行为运行）: {_throttle_err}")

# 面板
from bot.modules.panel import *
# 命令
from bot.modules.commands import *
# 其他
from bot.modules.extra import *
from bot.modules.callback import *
from bot.web import *


async def _on_startup():
    """bot 启动时启动 API 服务、调度器与开机任务"""
    from bot import config

    # 1) 启动 Web API 服务。
    # 原先该动作发生在 bot/web/__init__.py 的模块导入期（asyncio.get_event_loop() + create_task），
    # 既使用了已弃用的接口，也可能把任务调度到与 bot 不同（或已关闭）的事件循环上。
    try:
        await check.start()
    except SystemExit:
        # API 端口占用等致命错误沿用原有行为：直接退出
        raise
    except Exception as e:
        LOGGER.error(f"启动 API 服务失败: {e}")

    # 等待 bot 完全启动
    await asyncio.sleep(2)

    # 2) 启动 APScheduler（幂等）。
    # 原先在 bot/func_helper/scheduler.py 的模块导入期调用 start()，现已移除；
    # 各模块在导入期通过 scheduler.add_job 注册的任务此时只处于 pending 状态。
    # **不调用 start() 的后果是所有定时任务静默不执行**（备份、到期检测、活跃检测、
    # 榜单同步等全部失效），日志里只会出现一条 "调度器尚未启动" 的 warning。
    from bot.func_helper.scheduler import scheduler
    scheduler.start()

    # 3) 恢复同时播放限制检测任务
    # 这是该任务**唯一**的注册入口（面板开关回调只在运行时增删）。
    # 必须带 replace_existing：开机路径可能被重复执行（如热重载/重入），
    # 没有它会抛 ConflictingIdError 并被下面的 except 吞掉，导致任务静默缺失。
    try:
        if config.concurrent_play_limit_enabled:
            from bot.modules.extra.concurrent_play_monitor import check_concurrent_play_limit
            interval = config.concurrent_play_check_interval
            scheduler.add_job(check_concurrent_play_limit, 'interval', seconds=interval,
                              id='concurrent_play_check', replace_existing=True)
            LOGGER.info(f"已恢复同时播放限制检测任务，间隔 {interval} 秒")
    except Exception as e:
        LOGGER.error(f"恢复同时播放限制检测任务失败: {e}")

    # 3.5) 崩溃自恢复：把上次运行遗留的「临时封禁踢流」全部还原。
    #
    # 进程若在「禁用」与「还原」之间被杀（SIGKILL / 容器重建 / 断电），
    # try/finally 与后台还原任务都不会执行，用户会停在 IsDisabled=true 上被**永久锁死** ——
    # 而他只会看到"看不了片"，根本不知道要找管理员。
    # 这里**无条件**还原（不看 kick_until 是否到期）：宁可少踢一次流，
    # 也绝不能因为一次异常退出就把用户锁在门外。
    try:
        from bot.modules.extra.concurrent_play_monitor import restore_pending_kicks
        restored = await restore_pending_kicks(only_expired=False)
        if restored:
            LOGGER.warning(
                f"启动自恢复：已还原 {restored} 个遗留的临时封禁（上次运行未正常收尾）"
            )
    except Exception as e:
        LOGGER.error(f"启动自恢复临时封禁失败: {e}")

    # 4) 开机任务：设置命令菜单、清理重启状态、预热 peer 缓存。
    # 原先由 bot/modules/panel/sched_panel.py 在模块导入期用 loop.call_later 注册，
    # 导入期拿到的循环不一定等于 bot.run() 真正使用的循环，可能静默永不触发。
    try:
        from bot.modules.panel.sched_panel import startup_tasks
        await startup_tasks()
    except Exception as e:
        LOGGER.error(f"执行开机任务失败: {e}")


def _log_startup_failure(task: "asyncio.Task"):
    """后台启动任务的异常默认只会打印 "Task exception was never retrieved"，
    这里显式记录，避免启动阶段的问题被静默吞掉。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        LOGGER.error(f"启动任务异常: {exc!r}")


def _shutdown():
    """进程退出时显式关闭长连接资源（B-L5）。

    原先依赖 aiohttp 会话的 __del__ 与事件循环关闭时的告警，退出时可能打印
    "Unclosed client session" 并让未完成的请求悬空。这里在事件循环仍然可用时
    显式关闭 Emby 的会话。
    """
    from bot.func_helper.emby import emby

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if loop.is_closed() or loop.is_running():
        return

    async def _close():
        try:
            await emby.close()
        except Exception as e:
            LOGGER.error(f"关闭 Emby 连接失败: {e}")

    try:
        loop.run_until_complete(_close())
    except Exception as e:
        LOGGER.error(f"退出清理失败: {e}")


def main():
    """主函数：启动 bot 并恢复定时任务"""
    # 在事件循环中调度启动任务
    loop = asyncio.get_event_loop()
    task = loop.create_task(_on_startup())
    task.add_done_callback(_log_startup_failure)
    # 启动 bot（阻塞）
    try:
        bot.run()
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
