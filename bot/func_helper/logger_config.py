"""
logger_config - 

Author:susu
Date:2023/12/12
"""
import datetime
import pytz
from loguru import logger

# 转换为亚洲上海时区
shanghai_tz = pytz.timezone("Asia/Shanghai")
Now = datetime.datetime.now(shanghai_tz)
log_filename = f"log/log_{Now.strftime('%Y%m%d')}.txt"
log_format = "{time:YYYY-MM-DD HH:mm:ss.SSS ZZ} | {name} | {level} | {message}"

# D-L7：单个日志文件的大小上限。
# 旧配置只有 rotation="00:00"（按天），单日日志没有任何上限，
# 一次异常刷屏或 DEBUG 风暴就能把磁盘写满（本文件是 INFO 级别，但异常栈会成倍放大）。
LOG_MAX_BYTES = 50 * 1024 * 1024  # 50MB
_rotation_state = {"day": Now.strftime("%Y%m%d")}


def _rotation_condition(message, file):
    """轮转条件：跨天，或当前文件已超过 LOG_MAX_BYTES。

    loguru 的 rotation 只接受**一个**条件（字符串/数值/timedelta/time/可调用对象），
    不支持传列表，因此这里用一个可调用对象同时表达"按天 + 按大小"。
    其中按大小的判定与 loguru 内部 `Rotation.rotation_size` 完全一致。
    """
    record_day = message.record["time"].strftime("%Y%m%d")
    if record_day != _rotation_state["day"]:
        _rotation_state["day"] = record_day
        return True
    try:
        file.seek(0, 2)
        return file.tell() + len(message) > LOG_MAX_BYTES
    except (OSError, ValueError):
        return False


# 更新日志配置中的时间格式，确保记录的时间是东八区的时间
log_config = {
    "sink": log_filename,
    "format": log_format,  # 显示时区信息
    "level": "INFO",
    "rotation": _rotation_condition,  # 跨天或单文件超过 LOG_MAX_BYTES 时轮转
    "retention": "30 days",  # retention ：过滤旧文件的指令，在循环或程序结束期间会删除旧文件。
    "compression": "zip"  # D-L7：历史日志压缩归档，避免 log/ 目录无限膨胀
}
logger.add(**log_config)


def logu(name):
    """返回一个绑定名称的日志实例"""
    return logger.bind(name=name)
