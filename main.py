#! /usr/bin/python3
# -*- coding: utf-8 -*-

import asyncio
from bot import bot

# 面板
from bot.modules.panel import *
# 命令
from bot.modules.commands import *
# 其他
from bot.modules.extra import *
from bot.modules.callback import *
from bot.web import *


async def _on_startup():
    """bot 启动时恢复定时任务"""
    # 等待 bot 完全启动
    await asyncio.sleep(2)
    from bot import config, LOGGER
    try:
        if config.concurrent_play_limit_enabled:
            from bot.func_helper.scheduler import scheduler
            from bot.modules.extra.concurrent_play_monitor import check_concurrent_play_limit
            interval = config.concurrent_play_check_interval
            scheduler.add_job(check_concurrent_play_limit, 'interval', seconds=interval, id='concurrent_play_check')
            LOGGER.info(f"已恢复同时播放限制检测任务，间隔 {interval} 秒")
    except Exception as e:
        LOGGER.error(f"恢复同时播放限制检测任务失败: {e}")


def main():
    """主函数：启动 bot 并恢复定时任务"""
    # 在事件循环中调度启动任务
    loop = asyncio.get_event_loop()
    loop.create_task(_on_startup())
    # 启动 bot（阻塞）
    bot.run()


if __name__ == "__main__":
    main()
