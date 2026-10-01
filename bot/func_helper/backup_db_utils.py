import asyncio
import glob
import os
import shlex
from datetime import datetime

from bot import LOGGER


class BackupDBUtils:

    @staticmethod
    def _ensure_backup_dir(backup_dir):
        """确保备份目录存在，并在新建时收紧权限（B-H3④）。"""
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir, mode=0o700, exist_ok=True)

    @staticmethod
    def _remove_partial(path):
        """删除失败/不完整的备份文件，避免把残缺文件当成有效备份。"""
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as e:
            LOGGER.error(f"删除不完整的备份文件失败: {str(e)}")

    @staticmethod
    async def _run(argv, env=None, stdin_data=None, stdout=None):
        """
        以参数数组方式执行外部命令（B-H3②：不再用 create_subprocess_shell 拼接配置值）。
        返回 (returncode, stderr_text)。
        """
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=stdout if stdout is not None else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await process.communicate(stdin_data)
        return process.returncode, (stderr or b"").decode(errors="replace").strip()

    @staticmethod
    def _docker_dump_argv(container_name, user, database_name, backup_file_in_container, extra_flags):
        """
        构造容器内 mysqldump 的命令数组。
        密码经 stdin 注入容器内的 MYSQL_PWD，既不进 argv 也不进 shell 字符串（B-H3①②）。
        """
        inner = (
            'IFS= read -r __sakura_pw; '
            'MYSQL_PWD="$__sakura_pw"; export MYSQL_PWD; '
            'exec mysqldump {flags} --no-tablespaces -u{user} {db} > {out}'
        ).format(
            flags=" ".join(extra_flags),
            user=shlex.quote(str(user)),
            db=shlex.quote(str(database_name)),
            out=shlex.quote(str(backup_file_in_container)),
        )
        return ["docker", "exec", "-i", container_name, "sh", "-c", inner]

    @staticmethod
    # 数据库备份(mysql直装/本机含有mysql)
    async def backup_mysql_db(host, port, user, password, database_name, backup_dir, max_backup_count):
        # 如果文件夹不存在，就创建它
        BackupDBUtils._ensure_backup_dir(backup_dir)
        # 根据时间创建当前备份文件
        backup_file = os.path.join(backup_dir, f'{database_name}-{datetime.now().strftime("%Y-%m-%d-%H-%M-%S")}.sql')
        # B-H3①：密码用 MYSQL_PWD 环境变量传递，不再出现在命令行/进程列表里
        env = dict(os.environ)
        env["MYSQL_PWD"] = str(password)
        base_argv = ["mysqldump", f"-h{host}", "--no-tablespaces", f"-P{port}", f"-u{user}"]
        return_code = -1
        try:
            for extra_flags in ([], ["--skip-ssl"]):
                if extra_flags:
                    LOGGER.warning(f"BOT数据库备份失败，使用 skip-ssl方式尝试备份")
                with open(backup_file, "wb") as out:
                    return_code, stderr = await BackupDBUtils._run(
                        base_argv + extra_flags + [str(database_name)], env=env, stdout=out
                    )
                if return_code == 0:
                    break
            if return_code != 0:
                LOGGER.error(f"BOT数据库备份失败, error code: {return_code}")
                BackupDBUtils._remove_partial(backup_file)
                return None
            os.chmod(backup_file, 0o600)
            LOGGER.info(f"BOT数据库备份成功,文件保存为 {backup_file}")
            # 获取所有备份文件，并且通过时间进行排序
            all_backups = sorted(glob.glob(os.path.join(backup_dir, f'{database_name}-*.sql')))
            # 如果超过了当前的备份最大数量，则删除最久的一个
            while len(all_backups) > max_backup_count:
                os.remove(all_backups[0])
                all_backups.pop(0)
        except Exception as e:
            LOGGER.error(f"BOT数据库备份失败, error: {str(e)}")
            BackupDBUtils._remove_partial(backup_file)
            return None
        return backup_file

    @staticmethod
    # 数据库备份(docker)
    async def backup_mysql_db_docker(container_name, user, password, database_name, backup_dir, max_backup_count):
        # 如果文件夹不存在，就创建它
        BackupDBUtils._ensure_backup_dir(backup_dir)
        # 根据当前时间创建备份文件
        backup_file_in_container = f'{database_name}-{datetime.now().strftime("%Y-%m-%d-%H-%M-%S")}.sql'
        backup_file_on_host = os.path.join(backup_dir, backup_file_in_container)
        # 密码只经 stdin 传入容器内的 MYSQL_PWD
        password_stdin = (str(password) + "\n").encode()
        return_code = -1
        copied = False
        try:
            # 进入容器，使用mysqldump备份文件
            for extra_flags in ([], ["--skip-ssl"]):
                if extra_flags:
                    LOGGER.warning(f"BOT数据库备份失败，使用 skip-ssl方式尝试备份")
                return_code, stderr = await BackupDBUtils._run(
                    BackupDBUtils._docker_dump_argv(
                        container_name, user, database_name, backup_file_in_container, extra_flags
                    ),
                    stdin_data=password_stdin,
                )
                if return_code == 0:
                    break
            if return_code != 0:
                LOGGER.error(f"BOT数据库备份失败, error code: {return_code}")
                return None
            # 将容器中的备份文件复制到本地（B-H3③：检查返回码与文件是否存在，避免"假成功"）
            return_code, stderr = await BackupDBUtils._run(
                ["docker", "cp", f"{container_name}:{backup_file_in_container}", backup_file_on_host]
            )
            if return_code != 0 or not os.path.exists(backup_file_on_host):
                LOGGER.error(f"BOT数据库备份失败, docker cp 未成功, error code: {return_code}, {stderr}")
                BackupDBUtils._remove_partial(backup_file_on_host)
                return None
            copied = True
            os.chmod(backup_file_on_host, 0o600)
        except Exception as e:
            LOGGER.error(f"BOT数据库备份失败, error: {str(e)}")
            BackupDBUtils._remove_partial(backup_file_on_host)
            return None
        finally:
            # 删除容器中文件
            try:
                await BackupDBUtils._run(["docker", "exec", container_name, "rm", backup_file_in_container])
            except Exception as e:
                LOGGER.error(f"清理容器内备份文件失败, error: {str(e)}")
        if not copied:
            return None
        LOGGER.info(f"BOT数据库备份成功,文件保存为 {backup_file_on_host}")
        # 获取所有备份文件，并且通过时间进行排序
        all_backups = sorted(glob.glob(os.path.join(backup_dir, f'{database_name}-*.sql')))
        # 如果超过了当前的备份最大数量，则删除最久的一个
        while len(all_backups) > max_backup_count:
            os.remove(all_backups[0])
            all_backups.pop(0)
        return backup_file_on_host
