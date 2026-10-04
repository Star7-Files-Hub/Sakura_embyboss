import os

from bot import bot, owner, LOGGER, db_is_docker, db_docker_name, db_host, db_name, db_user, db_pwd, \
    db_backup_dir, db_backup_maxcount, db_port
from bot.func_helper.backup_db_utils import BackupDBUtils


def _backup_chat_id(raw_owner=None):
    """
    备份收件人硬校验：返回唯一合法的收件人（owner 私聊 id），不合法时返回 None。

    设计意图（安全不变量，改动前请先读这里）：
    - 备份收件人**只**来自 config 里的 owner，**不接受任何来自消息上下文的 chat id**
      （不使用 msg.chat.id / call.message.chat.id 之类）。因此即使 owner 本人在群里
      发 /backup_db、或把 bot 拉进群，数据库备份与 config.json 也只会发到 owner 私聊。
    - 必须是私聊：Telegram 里群组与频道的 id 一律为负数，owner < 0 说明被配置成了
      群组/频道，此时**拒绝发送**。config.json 含 bot_token / emby_api / db_pwd /
      tz_password / moviepilot.access_token 等全部密钥，一旦发到群里就永久留在
      群聊记录与每台已登录设备上，无法像改口令那样失效。
    - 0 不是合法的用户 id，同样拒绝。
    - 只接受整数 / 整数值的浮点 / 十进制数字串；**不用 `int(candidate)` 一把梭** ——
      `int(3.5) == 3`、`int(True) == 1` 会把含全部密钥的备份**静默发给另一个用户 id**。
    - 拒绝发送时备份文件仍保留在本地 db_backup 目录，只记 error，不抛异常。
    """
    candidate = owner if raw_owner is None else raw_owner
    if isinstance(candidate, bool) or candidate is None:
        chat_id = None
    elif isinstance(candidate, int):
        chat_id = candidate
    elif isinstance(candidate, float):
        # 只接受整数值的浮点（如 5608153118.0），3.5 这类会被截断成另一个 id，必须拒绝
        chat_id = int(candidate) if candidate.is_integer() else None
    elif isinstance(candidate, str):
        # 只接受 ASCII 数字，**不能直接 int(candidate)**：
        #   int("１２３") == 123（全角）、int("١٢٣") == 123（阿拉伯-印度数字）、
        #   int("1_000") == 1000（下划线）
        # 这些都会把含全部密钥的备份**静默发给另一个用户 id**。
        # 说明：生产环境 config.json 经 pydantic 校验，owner 已是 int，
        # 全角/下划线这类字符串根本到不了这里（实测 pydantic v2 会直接
        # ValidationError）；本判断是为了让这个安全校验在被单独调用/被测试
        # 直接调用时同样正确，不依赖上游替它把关。
        s = candidate.strip()
        chat_id = int(s) if (s.isascii() and s.isdigit()) else None
    else:
        chat_id = None
    if chat_id is None:
        LOGGER.error(
            f'备份收件人非法：owner={candidate!r} 不是合法的整数用户 id，无法确定收件人，'
            f'已拒绝发送备份（备份文件仍保留在本地 db_backup 目录）'
        )
        return None
    if chat_id < 0:
        LOGGER.error(
            f'备份收件人被配置成了群组/频道（owner={chat_id}，Telegram 群组与频道 id 一律为负数）。'
            f'数据库备份与 config.json 含全部密钥，绝不能发到群里，已拒绝发送；'
            f'请把 owner 改成你自己的用户 id（正数）。'
        )
        return None
    if chat_id == 0:
        LOGGER.error(
            f'备份收件人非法：owner=0 不是合法的用户 id，已拒绝发送备份；'
            f'请把 owner 改成你自己的用户 id（正数）。'
        )
        return None
    return chat_id


class DbBackupUtils:
    # 数据库的相关配置
    host = db_host
    user = db_user
    port = db_port
    password = db_pwd
    database_name = db_name
    backup_dir = db_backup_dir
    max_backup_count = db_backup_maxcount
    docker_mode = os.environ.get('DOCKER_MODE') == "1"
    docker_name = db_docker_name

    @classmethod
    async def backup_db(cls):
        backup_file = None
        # 如果是在docker模式下运行的此程序，使用BackupDBUtils.backup_mysql_db的方式备份数据库（此镜像中已经安装了mysqldump工具）
        if os.environ.get('DOCKER_MODE') == "1" or not db_is_docker:
            backup_file = await BackupDBUtils.backup_mysql_db(
                host=db_host,
                port=db_port,
                user=db_user,
                password=db_pwd,
                database_name=db_name,
                backup_dir=db_backup_dir,
                max_backup_count=db_backup_maxcount
            )
        elif db_is_docker:
            backup_file = await BackupDBUtils.backup_mysql_db_docker(
                container_name=db_docker_name,
                user=db_user,
                password=db_pwd,
                database_name=db_name,
                backup_dir=db_backup_dir,
                max_backup_count=db_backup_maxcount
            )
        return backup_file

    @staticmethod
    async def auto_backup_db():
        LOGGER.info("BOT数据库备份开始")
        backup_file = await DbBackupUtils.backup_db()
        if backup_file is not None:
            LOGGER.info(f'BOT数据库备份完毕')
            # 收件人硬校验：只认 config 里的 owner，且必须是私聊（正数）；
            # 不接受任何来自消息上下文的 chat id（见 _backup_chat_id() 的 docstring）。
            # 校验不通过时两个文件都不发，只记 error，备份文件仍留在本地 db_backup 目录。
            #
            # 这里**直接调用**，不做「取不到校验函数就退化成宽松判断」的兜底：
            # 那种兜底会让 _backup_chat_id 被改名/误删时静默降级成较弱的检查，
            # 而不是当场 NameError 炸出来 —— 对一个「绝不能发错人」的安全校验来说，
            # 静默降级比直接失败危险得多。单元测试的隔离执行环境改为同时抽出
            # _backup_chat_id（见 tests/test_backup_sends_config.py）。
            chat_id = _backup_chat_id()
            if chat_id is None:
                LOGGER.error(
                    f'备份收件人非法，已拒绝发送数据库备份与 config.json'
                    f'（文件仍保留在本地 db_backup 目录，请检查 config.json 里的 owner）'
                )
            else:
                # 数据库备份与配置备份**分开发送、各自捕获异常**：
                # 一个发不出去不应该把另一个也拖掉（原来用 asyncio.gather 会一起抛，
                # 结果两个文件都当失败处理）。
                try:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=backup_file,
                        caption=f'BOT数据库备份完毕',
                        disable_notification=True  # 勿打扰
                    )
                except Exception as e:
                    LOGGER.info(f'发送到owner失败，文件保存在本地:{e}')

                # 配置备份：只发给 owner 本人。
                # ⚠️ config.json 含 bot_token / emby_api / db_pwd / tz_password /
                # moviepilot.access_token 等全部密钥，请勿转发、勿拉入群组或频道 ——
                # 消息一旦发出就永久留在聊天记录与每台已登录设备上，无法像改口令那样失效。
                try:
                    await bot.send_document(
                        chat_id=chat_id,
                        document='config.json',
                        caption=f'config备份完毕',
                        disable_notification=True  # 勿打扰
                    )
                except Exception as e:
                    LOGGER.info(f'发送 config.json 到 owner 失败:{e}')
        else:
            LOGGER.error(f'BOT数据库手动备份失败，请尽快检查相关配置')
