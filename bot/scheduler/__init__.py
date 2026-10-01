# D-M7：本包只负责"导出定时任务函数"，不在导入期启动调度器。
# 任务真正被注册/启动的时机是 main.py 的启动钩子 `_on_startup()`：
#     from bot.func_helper.scheduler import scheduler
#     scheduler.start()          # 显式启动（幂等），之后 add_job 才会生效
# 在此之前 bot/modules/panel/sched_panel.py 的 set_all_sche() 只是把任务
# 暂存进 APScheduler 的 pending 列表。
from .userplays_rank import Uplaysinfo
from .backup_db import DbBackupUtils
from .bot_commands import BotCommands
from .check_ex import check_expired
from .check_restart import check_restart
from .ranks_task import week_ranks, day_ranks
from .sync_favorites import sync_favorites
from .sync_mp_download import sync_download_tasks
from .partition_access import check_partition_access
