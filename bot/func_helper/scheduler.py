import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot import LOGGER
from bot.func_helper.utils import Singleton


class Scheduler(metaclass=Singleton):
    # D-M5：APScheduler 的任务默认值**只能**通过 `job_defaults` 传入。
    # 旧代码写的是 `AsyncIOScheduler(..., max_instances=5, misfire_grace_time=60)`，
    # 这两个键不在 APScheduler 的配置里（BaseScheduler._configure 只读取
    # config['job_defaults']），因此被**静默忽略**，真正生效的是框架默认值
    # coalesce=True / max_instances=1 / misfire_grace_time=1。
    # 这里改为显式 job_defaults，让行为与注释一致：
    #   - coalesce=True       积压的多次触发只补跑一次（对有副作用的批处理任务很关键）
    #   - max_instances=1     同一个 job 不允许并发重入
    #   - misfire_grace_time  错过触发后仍允许补跑的宽限秒数（默认 1 秒太紧，
    #                         一次 GC/网络抖动就会把整点任务直接丢掉）
    DEFAULT_JOB_DEFAULTS = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 60,
    }

    def __init__(self, timezone='Asia/Shanghai', misfire_grace_time=60, event_loop=None):
        # 创建一个AsyncIOScheduler对象，并传入时区、任务默认值和事件循环参数
        job_defaults = dict(self.DEFAULT_JOB_DEFAULTS)
        job_defaults["misfire_grace_time"] = misfire_grace_time
        self.SCHEDULER = AsyncIOScheduler(timezone=timezone, job_defaults=job_defaults,
                                          event_loop=event_loop or asyncio.get_event_loop())
        # D-M7：不再在模块导入期 start()。
        # 导入期启动会在事件循环还没跑起来时就把 scheduler 绑定到某个 loop 上，
        # 之后再添加任务属于"先跑后配"，启动顺序不可控。
        # 现在由 main.py 的启动钩子显式调用 self.start()（幂等）。
        self._warned_pending = False
        # 设置日志级别为INFO
        # logging.basicConfig(level=logging.INFO)

    def start(self):
        """
        显式启动调度器（幂等）。

        必须在事件循环就绪之后调用，例如 main.py 的 `_on_startup()` 里：
            from bot.func_helper.scheduler import scheduler
            scheduler.start()
        未启动时 `add_job` 只会把任务暂存在 APScheduler 的 pending 列表里
        （APScheduler 会打印 "Adding job tentatively"），不会真正触发。
        """
        if self.SCHEDULER.running:
            LOGGER.info("Scheduler 已在运行，跳过重复启动。")
            return
        try:
            self.SCHEDULER.start()
            LOGGER.info("Scheduler 已启动。")
        except Exception as e:
            LOGGER.error(f"Failed to start the scheduler: {e}")

    # 函数、触发器、
    def add_job(self, func, trigger, **kwargs):
        # 调用调度器的add_job方法，添加定时任务
        try:
            self.SCHEDULER.add_job(func, trigger, **kwargs)
            if not self.SCHEDULER.running and not self._warned_pending:
                self._warned_pending = True
                LOGGER.warning(
                    "调度器尚未启动，定时任务只会暂存（pending）。"
                    "请确认 main.py 的启动钩子调用了 `scheduler.start()`，"
                    "否则所有定时任务都不会执行。"
                )
            LOGGER.info(f"Added a job: {func.__name__} with {trigger} trigger and {kwargs} arguments.")
        except Exception as e:
            LOGGER.error(f"Failed to add a job: {e}")

    def remove_job(self, job_id=None, jobstore=None):
        # 调用调度器的remove_job方法，移除一个定时任务
        try:
            self.SCHEDULER.remove_job(job_id, jobstore)
            LOGGER.info(f"Removed a job: {job_id} from {jobstore}.")
        except Exception as e:
            LOGGER.error(f"Failed to remove a job: {e}")

    def shutdown(self):
        # 调用调度器的shutdown方法，关闭调度器
        try:
            self.SCHEDULER.shutdown()
            LOGGER.info("Shutdown the scheduler successfully.")
        except Exception as e:
            LOGGER.error(f"Failed to shutdown the scheduler: {e}")

    @property
    def running(self):
        # 返回调度器是否正在运行
        return self.SCHEDULER.running

    @property
    def paused(self):
        # 返回调度器是否处于暂停状态
        return self.SCHEDULER.state == 2

    def pause(self): \
            # 调用调度器的pause方法，暂停调度器
        try:
            self.SCHEDULER.pause()
            LOGGER.info("Paused the scheduler successfully.")
        except Exception as e:
            LOGGER.error(f"Failed to pause the scheduler: {e}")

    def resume(self):
        # 调用调度器的resume方法，恢复调度器
        try:
            self.SCHEDULER.resume()
            LOGGER.info("Resumed the scheduler successfully.")
        except Exception as e:
            LOGGER.error(f"Failed to resume the scheduler: {e}")

    def modify_job(self, job_id, **changes):
        # 调用调度器的modify_job方法，修改某个任务的属性
        try:
            self.SCHEDULER.modify_job(job_id, **changes)
            LOGGER.info(f"Modified a job: {job_id} with {changes} changes.")
        except Exception as e:
            LOGGER.error(f"Failed to modify a job: {e}")


scheduler = Scheduler()
# D-M7：调度器的启动由 main.py 的启动钩子显式完成：
#     from bot.func_helper.scheduler import scheduler
#     scheduler.start()
# 这里不再自动 start()，避免"模块导入期就启动"造成的事件循环绑定与启动顺序问题。
# scheduler.add_job(check_expired, 'cron', hour=1, id='check_expired')
