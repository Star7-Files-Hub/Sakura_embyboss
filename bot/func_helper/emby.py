#! /usr/bin/python3
# -*- coding:utf-8 -*-
"""
emby的api操作方法 - 使用aiohttp重构版本
"""
import asyncio
import os
import random
import re
import urllib.parse

import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any, List, Union
from contextlib import asynccontextmanager

from bot import emby_url, emby_api, emby_block, extra_emby_libs, LOGGER, config
from bot.sql_helper.sql_emby import sql_update_emby, sql_get_emby, Emby
from bot.func_helper.utils import pwd_create, convert_runtime, cache, Singleton


async def is_emby_admin(emby_id: str):
    """
    判断该用户是否是 **Emby 侧** 管理员（`Policy.IsAdministrator`）。

    为什么需要它：用户明确要求「Emby 管理员永久豁免」。并发监控原本只靠
    `judge_admins()`（站长 owner + `config.admins`）判豁免，而 Emby 侧的
    `IsAdministrator` 不在这个定义里。1 号机的 `admin` 账号恰好**没有在 bot
    里建档**，所以靠「查不到记录就跳过」侥幸豁免 —— 一旦某个 Emby 管理员
    同时在 bot 里建了档（例如 `lv='b'`），超限时就会走到封禁，而封禁会把
    `IsAdministrator` 写成 false（`create_policy(admin=False)`），
    等于把管理员**永久降权**，所以必须显式豁免，不能再依赖巧合。

    :return: True=是 Emby 管理员；False=不是；None=取不到（未知）
    """
    try:
        result = await emby._request('GET', f'/emby/Users/{emby_id}')
        if not result.success or not isinstance(result.data, dict):
            return None
        policy = result.data.get('Policy')
        if not isinstance(policy, dict) or policy.get('IsAdministrator') is None:
            return None
        return bool(policy['IsAdministrator'])
    except Exception as e:
        LOGGER.warning(
            f"判断 Emby 管理员身份失败 emby_id={emby_id}: {type(e).__name__}: {e}"
        )
        return None


def create_policy(admin=False, disable=False, limit: int = 2, block: list = None):
    """
    创建用户策略
    :param admin: bool 是否开启管理员
    :param disable: bool 是否禁用
    :param limit: int 同时播放流的默认值，修改2 -> 3 any都可以
    :param block: list 默认将 播放列表 屏蔽
    :return: policy 用户策略
    """
    if block is None:
        block = ['播放列表'] + extra_emby_libs
    
    policy = {
        "IsAdministrator": admin,
        "IsHidden": True,
        "IsHiddenRemotely": True,
        "IsDisabled": disable,
        "EnableRemoteControlOfOtherUsers": False,
        "EnableSharedDeviceControl": False,
        "EnableRemoteAccess": True,
        "EnableLiveTvManagement": False,
        "EnableLiveTvAccess": True,
        "EnableMediaPlayback": True,
        "EnableAudioPlaybackTranscoding": False,
        "EnableVideoPlaybackTranscoding": False,
        "EnablePlaybackRemuxing": False,
        "EnableContentDeletion": False,
        "EnableContentDownloading": False,
        "EnableSubtitleDownloading": False,
        "EnableSubtitleManagement": False,
        "EnableSyncTranscoding": False,
        "EnableMediaConversion": False,
        "EnableAllDevices": True, 
        "SimultaneousStreamLimit": limit,
        "BlockedMediaFolders": block,
        "AllowCameraUpload": False,  # 新版api 控制开关相机上传
    }
    return policy


def pwd_policy(embyid: str, stats: bool = False, new: str = None) -> Dict[str, Any]:
    """
    创建密码策略
    :param embyid: str 修改的emby_id
    :param stats: bool 是否重置密码
    :param new: str 新密码
    :return: policy 密码策略
    """
    if new is None:
        policy = {
            "Id": str(embyid),
            "ResetPassword": stats,
        }
    else:
        policy = {
            "Id": str(embyid),
            "NewPw": str(new),
        }
    return policy


# ──────────────────────────────────────────────────────────────────────────────
# 会话轮询窗口（2026-10-xx 瘦身）
# ──────────────────────────────────────────────────────────────────────────────

# `GET /emby/Sessions` 裸调实测：**2,970,545 B / 4037 条 session / 7.70s**，其中真正
# 有 `NowPlayingItem` 的只有 7~8 条（4000 多条是永不清理的僵尸会话，只在进程内存里）。
# 加 `?ActiveWithinSeconds=300` 后实测 89,940 B / 75 条 / **0.66s** —— 体积 1/33、耗时
# 1/12，且**语义等价**：本文件以及所有调用方（server_panel / watching /
# concurrent_play_monitor / line_report）拿到列表后都只统计/挑选 `NowPlayingItem`
# 非空的会话，僵尸会话本来就被丢弃。
#
# 权威实现是 `register_throttle.sessions_endpoint()`（全仓库 4 处会话调用点统一走它，
# 窗口可用 `register_session_active_seconds` 配置，0 = 退回裸端点），本文件优先调用它；
# 下面的常量/函数**只在限流模块不可用时兜底**，默认 300。纯优化参数，任何异常都回落，
# 绝不因为它打断巡检。
SESSIONS_ACTIVE_WITHIN_SECONDS_DEFAULT = 300

# 兜底时读取窗口的配置名/环境变量名：与 register_throttle 的键名保持一致，
# 免得同一个参数在两处要用两种写法。
_SESSIONS_WINDOW_ATTRS = ("register_session_active_seconds", "session_active_seconds",
                          "sessions_active_within_seconds")
_SESSIONS_WINDOW_ENVS = ("EMBY_THROTTLE_SESSION_ACTIVE_SECONDS",
                         "SAKURA_SESSIONS_ACTIVE_WITHIN_SECONDS")


def _sessions_active_within_seconds() -> int:
    """
    **兜底用**：取会话轮询的活动窗口（秒）。优先级：环境变量 > `_open` > 默认 300。

    返回 0 表示显式要求"不加参数"。任何异常/非法值都回落到默认值。
    """
    for env_key in _SESSIONS_WINDOW_ENVS:
        raw = os.environ.get(env_key)
        if raw:
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                break
    try:
        from bot import _open as _open_obj
        for attr in _SESSIONS_WINDOW_ATTRS:
            if hasattr(_open_obj, attr):
                return int(getattr(_open_obj, attr))
    except Exception:
        pass
    return SESSIONS_ACTIVE_WITHIN_SECONDS_DEFAULT


def _fallback_sessions_endpoint() -> str:
    """
    **兜底用**：`register_throttle.sessions_endpoint()` 不可用时的等价实现。
    """
    seconds = _sessions_active_within_seconds()
    if seconds <= 0:
        return '/emby/Sessions'
    return f'/emby/Sessions?ActiveWithinSeconds={seconds}'


class EmbyApiResult:
    """API 结果统一封装"""
    def __init__(self, success: bool, data: Any = None, error: str = None):
        self.success = success
        self.data = data
        self.error = error
    
    def __bool__(self):
        return self.success


class Embyservice(metaclass=Singleton):
    """
    Emby API 服务类 - 使用 aiohttp 重构版本
    提供统一的异步HTTP请求、错误处理、重试机制和资源管理
    """

    def __init__(self, url: str, api_key: str, timeout: int = 10, max_retries: int = 3):
        """
        初始化 Emby 服务
        :param url: Emby 服务器地址
        :param api_key: API 密钥
        :param timeout: 请求超时时间（秒）
        :param max_retries: 最大重试次数
        """
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.max_retries = max_retries
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        
        # 请求头配置
        self.headers = {
            'accept': 'application/json',
            'content-type': 'application/json',
            'X-Emby-Token': self.api_key,
            'X-Emby-Client': 'Sakura BOT',
            'X-Emby-Device-Name': 'Sakura BOT',
            'X-Emby-Client-Version': '1.0.0',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36 Edg/114.0.1823.82'
        }
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self):
        """
        异步上下文管理器，管理 aiohttp 会话
        自动处理会话的创建和复用
        """
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(
                    limit=100,  # 连接池大小
                    limit_per_host=30,  # 每个主机的连接数
                    keepalive_timeout=60,  # 保持连接时间
                    enable_cleanup_closed=True
                )
                self._session = aiohttp.ClientSession(
                    headers=self.headers,
                    timeout=self.timeout,
                    connector=connector,
                    raise_for_status=False  # 手动处理HTTP状态码
                )
        
        try:
            yield self._session
        except Exception as e:
            LOGGER.error(f"会话使用异常: {str(e)}")
            raise

    async def close(self):
        """关闭会话并清理资源（B-L5：与 session() 共用锁，避免对端拿到已关闭的会话）"""
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
                LOGGER.info("Emby 服务会话已关闭")

    @staticmethod
    def _mask_url(url: str) -> str:
        """日志脱敏：隐藏 URL 中的 api_key（B-H2）"""
        return re.sub(r'(api_key=)[^&\s]+', r'\1***', str(url), flags=re.IGNORECASE)

    # B-M2：get_emby_report 的 ItemType 白名单
    _ALLOWED_ITEM_TYPES = ('Movie', 'Episode', 'Series', 'Season', 'Audio', 'Video', 'MusicVideo', 'Trailer')

    @staticmethod
    def _sanitize_like_keyword(keyword: str, max_length: int = 64) -> str:
        """
        B-M3：LIKE 关键词白名单过滤。
        只保留各语言字母/数字与少量安全符号，剔除引号、反斜杠、注释符以及 LIKE 元字符（% _），
        因此不需要依赖 ESCAPE 子句，也不会被管理员输入的 '%' 放大成全表匹配。
        """
        if not keyword:
            return ""
        safe_chars = set(" .-+/@()[]:")
        cleaned = "".join(ch for ch in str(keyword)[:max_length] if ch.isalnum() or ch in safe_chars)
        return cleaned.strip()

    async def _request(self, method: str, endpoint: str, timeout: aiohttp.ClientTimeout = None,
                       **kwargs) -> EmbyApiResult:
        """
        统一的HTTP请求方法，包含重试机制和错误处理
        :param method: HTTP方法
        :param endpoint: API端点
        :param timeout: 可选的本次请求超时（不改写单例共享超时，见 B-H4）
        :param kwargs: 请求参数
        :return: EmbyApiResult
        """
        url = f"{self.url}{endpoint}"
        if timeout is not None:
            kwargs.setdefault('timeout', timeout)

        # B-M1：只对幂等方法重试，避免 POST 建号等非幂等请求被重复执行
        idempotent = method.upper() in ('GET', 'HEAD', 'OPTIONS', 'PUT', 'DELETE')
        max_attempts = max(1, int(self.max_retries)) if idempotent else 1

        for attempt in range(max_attempts):
            try:
                async with self.session() as session:
                    async with session.request(method, url, **kwargs) as response:
                        # 检查HTTP状态码
                        if response.status in [200, 204]:
                            # 处理不同的响应类型
                            if response.content_type == 'application/json':
                                try:
                                    data = await response.json()
                                    return EmbyApiResult(True, data)
                                except Exception as e:
                                    LOGGER.error(f"JSON解析失败: {str(e)}")
                                    return EmbyApiResult(False, error=f"JSON解析失败: {str(e)}")
                            else:
                                # 处理二进制内容（如图片）
                                content = await response.read()
                                return EmbyApiResult(True, content)
                        
                        elif response.status == 404:
                            return EmbyApiResult(False, error="资源不存在")
                        elif response.status == 401:
                            return EmbyApiResult(False, error="认证失败，请检查API密钥")
                        elif response.status == 403:
                            return EmbyApiResult(False, error="权限不足")
                        else:
                            error_msg = f"HTTP {response.status}"
                            try:
                                error_text = await response.text()
                                if error_text:
                                    error_msg += f": {error_text}"
                            except Exception:
                                pass

                            # 429 与 5xx 属于可重试错误，其余 4xx 视为终态
                            retryable_status = response.status == 429 or response.status >= 500
                            retry_suffix = ""
                            if retryable_status and attempt + 1 < max_attempts:
                                retry_suffix = f" (尝试 {attempt + 1}/{max_attempts})"
                            LOGGER.warning(f"API请求失败: {method} {self._mask_url(url)} - {error_msg}{retry_suffix}")
                            if not retryable_status or attempt == max_attempts - 1:
                                return EmbyApiResult(False, error=error_msg)
            
            except asyncio.TimeoutError:
                LOGGER.warning(f"请求超时 (尝试 {attempt + 1}/{max_attempts}): {self._mask_url(url)}")
                if attempt == max_attempts - 1:
                    return EmbyApiResult(False, error="请求超时")
            
            except aiohttp.ClientError as e:
                LOGGER.error(f"网络请求异常 (尝试 {attempt + 1}/{max_attempts}): {str(e)}")
                if attempt == max_attempts - 1:
                    return EmbyApiResult(False, error=f"网络请求失败: {str(e)}")
            
            except Exception as e:
                # 未知异常不重试（可能是代码错误），避免放大故障
                LOGGER.error(f"未知异常 (尝试 {attempt + 1}/{max_attempts}): {str(e)}")
                return EmbyApiResult(False, error=f"未知错误: {str(e)}")

            # 指数退避 + 抖动
            await asyncio.sleep(min(8.0, float(2 ** attempt)) + random.uniform(0, 0.3))
        
        return EmbyApiResult(False, error="达到最大重试次数")

    async def _delete_orphan_account(self, user_id: str, name: str = None) -> None:
        """
        B-H1：删除创建流程中途失败时残留的孤儿账号（best-effort，失败只记录日志）
        """
        try:
            if await self.emby_del(emby_id=user_id):
                LOGGER.warning(f"已回滚删除创建失败的账号: {name} (ID: {user_id})")
            else:
                LOGGER.error(f"回滚删除账号失败，需人工清理孤儿账号: {name} (ID: {user_id})")
        except Exception as e:
            LOGGER.error(f"回滚删除账号异常: {name} (ID: {user_id}) - {str(e)}")

    # ── 建号快路径（7 次 HTTP → 3 次）────────────────────────────────────────

    @staticmethod
    def _load_throttle_fast_path():
        """
        惰性取 `register_throttle` 模块（建号快路径要复用它已有的 TTL 缓存/单飞）。

        为什么要惰性 import：`register_throttle.install()` 会替换
        `Embyservice._request`，而它内部又 `from bot.func_helper.emby import ...`，
        顶层 import 会构成循环导入。这里在函数内 import 并 try/except 兜底。

        **返回 None 表示"快路径不可用"**（模块没装/导入报错/缺少所需函数），
        调用方必须回退原来的多请求路径 —— 建号绝不能因为限流模块而失败。
        """
        try:
            from bot.func_helper import register_throttle
        except Exception as e:
            LOGGER.debug(f"register_throttle 不可用，建号走原多请求路径: {type(e).__name__}: {e}")
            return None
        if not hasattr(register_throttle, "cached_virtual_folders"):
            LOGGER.debug("register_throttle 缺少 cached_virtual_folders，建号走原多请求路径")
            return None
        return register_throttle

    @staticmethod
    async def _fetch_cached_folder_ids(throttle) -> Optional[Dict[str, str]]:
        """
        取媒体库映射 `{guid: name}`（走 register_throttle 的 TTL 缓存 + 单飞），
        替代原来每个账号都发一次的 `GET /emby/Library/VirtualFolders`。

        **返回 None 一律表示"快路径不可用，必须整条回退原逻辑"**，共两种情况：

        1. 缓存函数抛异常（限流模块自己坏了）；
        2. **媒体库列表为空** —— 这是最关键的安全边界：如果在这种情况下照常算策略，
           就会写出 `EnableAllFolders=False` + `EnabledFolders=[]`，而 Emby 里
           `EnabledFolders=[]` 表示"可见的库一个都没有"（不是"全部库"），用户会被
           **彻底锁死**：新建账号打开就是空库，且没有任何报错。所以列表为空时宁可
           回退到原来的「先写 create_policy（EnableAllFolders 保持 Emby 默认 true）
           再由 hide_folders_by_names 处理」路径，也绝不写空 EnabledFolders。
        """
        try:
            libs = await throttle.cached_virtual_folders()
        except Exception as e:
            LOGGER.warning(f"cached_virtual_folders 异常，建号回退原多请求路径: {type(e).__name__}: {e}")
            return None
        if not isinstance(libs, dict) or not libs:
            LOGGER.warning("媒体库列表为空，建号回退原多请求路径（拒绝写出锁死用户的空 EnabledFolders）")
            return None
        return libs

    @staticmethod
    def _build_full_policy(libs: Dict[str, str]) -> Dict[str, Any]:
        """
        一次算全的建号策略，**逐字等价**于原来「写两次策略」的最终生效值。

        原路径有两个分支，必须都复刻（这是"等价"的依据，不是拍脑袋）：

        第 3 步恒为 `create_policy(False, False)`：
            `BlockedMediaFolders = ['播放列表'] + extra_emby_libs`，
            且**不带** `EnableAllFolders` / `EnabledFolders` —— Emby 的 UserPolicy
            这两个字段保持原值（新账号默认 `true` / 空），即"全部库可见"。

        第 4 步 `hide_folders_by_names(emby_block + extra_emby_libs)`：

        分支甲 —— **两个名字在 Emby 里都找不到**（`get_folder_ids_by_names()` 返回空）：
            `hide_folders_by_names()` 直接 `return True`，**什么都不写**。
            最终状态 = 只有第 3 步那次 Policy：`EnableAllFolders` 仍是 `true`。
            ⚠️ 线上就是这一支：`emby_block=['nsfw']`、`extra_emby_libs=['电视']`，
               而真实库名是 '⚔️国产·动漫' / '📺日韩·剧集' 等，一个都对不上。
            所以这里**绝不能**顺手改成 `EnableAllFolders=False` + 显式 EnabledFolders ——
            那样虽然"今天看得见的库一样多"，但**以后新加的媒体库对这些用户不会自动可见**，
            是与现状不同的行为（需要靠 `/embylibs_all` 之类的手动同步补救）。

        分支乙 —— 名字能对上：
            `new_enabled = 当前启用(此时=全部库) − 被隐藏`、
            `new_blocked = dedup(第3步名单 ∪ emby_block ∪ extra_emby_libs)`、
            `EnableAllFolders=False`。

        :param libs: `{guid: name}`，必须是**非空**（空的情况见 `_fetch_cached_folder_ids`）
        """
        policy = create_policy(False, False)

        # 第 4 步真正要隐藏的名字（顺序与 hide_folders_by_names 的入参一致）
        hide_names = list(dict.fromkeys(list(emby_block or []) + list(extra_emby_libs or [])))
        hide_ids = {guid for guid, lib_name in libs.items() if lib_name in hide_names}

        if not hide_ids:
            # 分支甲：与 `hide_folders_by_names()` 提前 return True 逐字等价。
            # 只写第 3 步的策略，不碰 EnableAllFolders / EnabledFolders。
            return policy

        # 分支乙：一次写全，等价于"第3步 + 第4步"的最终值
        blocked_names = list(dict.fromkeys(
            ['播放列表'] + list(extra_emby_libs or []) + list(emby_block or [])
        ))
        policy.update({
            'BlockedMediaFolders': blocked_names,
            'EnableAllFolders': False,
            'EnabledFolders': [guid for guid in libs if guid not in hide_ids],
        })
        return policy

    async def emby_create(self, name: str, days: int) -> Union[Tuple[str, str, datetime], bool]:
        """
        创建 Emby 账户
        :param name: 用户名
        :param days: 有效天数
        :return: (用户ID, 密码, 过期时间) 或 False

        ── 请求数（2026-10-xx 瘦身）──
        快路径 **3 次 HTTP**，其中 Policy 只写一次、媒体库列表走 TTL 缓存：
            POST /Users/New → POST /Users/{id}/Password → POST /Users/{id}/Policy(一次写全)
        原路径 7 次：
            POST /Users/New → POST Password → POST Policy
            → GET /Users/{id} → GET /Library/VirtualFolders → GET /Users/{id} → POST Policy

        只要 `register_throttle.cached_virtual_folders()` 拿不到（未挂载/抛异常）或
        媒体库列表为空，就**整条回退**到原路径：返回值、日志、失败时的
        `_delete_orphan_account` 回滚语义都与改造前完全一致。
        """
        try:
            expiry_date = datetime.now() + timedelta(days=days)

            # 快路径可用性在**建号之前**判定：拿不到就从头走原路径，避免白建一个号再回滚。
            throttle = self._load_throttle_fast_path()

            # 1. 创建用户
            LOGGER.info(f"开始创建用户: {name}")
            result = await self._request('POST', '/emby/Users/New', json={"Name": name})
            if not result.success:
                LOGGER.error(f"创建用户失败: {result.error}")
                return False
            
            user_id = result.data.get("Id")
            if not user_id:
                LOGGER.error("无法获取用户ID")
                return False
            
            fast_policy_written = False
            # B-H1：建号之后的步骤失败必须删除已建账号，避免留下占用用户名的孤儿账号
            try:
                # 2. 设置密码
                password = await pwd_create(8)
                pwd_data = pwd_policy(user_id, new=password)
                result = await self._request('POST', f'/emby/Users/{user_id}/Password', json=pwd_data)
                if not result.success:
                    LOGGER.error(f"设置密码失败: {result.error}")
                    await self._delete_orphan_account(user_id, name)
                    return False
                
                # 3. 设置策略
                # 快路径：媒体库列表走缓存，BlockedMediaFolders/EnableAllFolders/
                # EnabledFolders 一次写全（等价于原「写两次策略」的最终值）。
                # 拿不到媒体库列表（含"列表为空"）→ 退回原策略对象，第 4 步再补。
                libs = await self._fetch_cached_folder_ids(throttle) if throttle is not None else None
                policy = self._build_full_policy(libs) if libs is not None else create_policy(False, False)
                result = await self._request('POST', f'/emby/Users/{user_id}/Policy', json=policy)
                if not result.success:
                    LOGGER.error(f"设置策略失败: {result.error}")
                    await self._delete_orphan_account(user_id, name)
                    return False
                fast_policy_written = libs is not None
            except Exception as e:
                LOGGER.error(f"创建用户后续步骤异常: {name} (ID: {user_id}) - {str(e)}")
                await self._delete_orphan_account(user_id, name)
                return False

            # 4. 隐藏 emby_block 和 extra_emby_libs 媒体库
            if fast_policy_written:
                # 快路径已在第 3 步一次写全（策略只写了这一次），不再走
                # hide_folders_by_names 的「GET /Users/{id} + GET /VirtualFolders + 再写一次 Policy」。
                LOGGER.debug(f"快路径已一次写全媒体库策略，跳过 hide_folders_by_names: {user_id}")
            else:
                try:
                    # 使用封装的隐藏方法
                    block_libs = emby_block + extra_emby_libs
                    result = await self.hide_folders_by_names(user_id, block_libs)
                    if not result:
                        LOGGER.warning(f"设置媒体库权限失败: {user_id}，但用户已创建成功")
                except Exception as e:
                    # 如果设置媒体库权限失败，记录错误但不影响用户创建
                    LOGGER.error(f"设置媒体库权限异常: {name} (ID: {user_id}) - {str(e)}")
            
            LOGGER.info(f"成功创建用户: {name} (ID: {user_id})")
            return user_id, password, expiry_date
            
        except Exception as e:
            LOGGER.error(f"创建用户异常: {name} - {str(e)}")
            return False

    async def emby_del(self, emby_id: str) -> bool:
        """
        删除 Emby 账户
        :param user_id: 用户ID
        :return: 是否成功
        """
        try:
            LOGGER.info(f"开始删除用户: {emby_id}")
            result = await self._request('DELETE', f'/emby/Users/{emby_id}')
            if result.success:
                LOGGER.info(f"成功删除用户: {emby_id}")
                return True
            else:
                LOGGER.error(f"删除用户失败: {emby_id} - {result.error}")
                return False
        except Exception as e:
            LOGGER.error(f"删除用户异常: {emby_id} - {str(e)}")
            return False

    async def emby_reset(self, emby_id: str, new_password: str = None) -> bool:
        """
        重置用户密码
        :param user_id: 用户ID
        :param new_password: 新密码，为空则重置为无密码
        :return: 是否成功
        """
        try:
            LOGGER.info(f"开始重置密码: {emby_id}")
            
            # 第一步：重置密码
            pwd_data = pwd_policy(emby_id, stats=True, new=None)
            result = await self._request('POST', f'/emby/Users/{emby_id}/Password', json=pwd_data)
            if not result.success:
                LOGGER.error(f"重置密码失败: {emby_id} - {result.error}")
                return False
            
            if new_password is None:
                # 更新数据库记录为无密码
                if sql_update_emby(Emby.embyid == emby_id, pwd=None):
                    LOGGER.info(f"成功重置密码为空: {emby_id}")
                    return True
                else:
                    LOGGER.error(f"更新数据库失败: {emby_id}")
                    return False
            else:
                # 设置新密码
                pwd_data2 = pwd_policy(emby_id, new=new_password)
                result = await self._request('POST', f'/emby/Users/{emby_id}/Password', json=pwd_data2)
                if not result.success:
                    LOGGER.error(f"设置新密码失败: {emby_id} - {result.error}")
                    return False
                
                # 更新数据库
                if sql_update_emby(Emby.embyid == emby_id, pwd=new_password):
                    LOGGER.info(f"成功重置密码: {emby_id}")
                    return True
                else:
                    LOGGER.error(f"更新数据库失败: {emby_id}")
                    return False
                    
        except Exception as e:
            LOGGER.error(f"重置密码异常: {emby_id} - {str(e)}")
            return False

    async def emby_block(self, emby_id: str, stats: int = 0, block: list = None) -> bool:
        """
        设置用户媒体库访问权限
        :param emby_id: 用户ID
        :param stats: 0-阻止访问，1-允许访问
        :param block: 要阻止的媒体库列表
        :return: 是否成功
        """
        try:
            if block is None:
                block = emby_block

            if stats == 0:
                policy = create_policy(False, False, block=block)
            else:
                policy = create_policy(False, False)
                
            result = await self._request('POST', f'/emby/Users/{emby_id}/Policy', json=policy)
            if result.success:
                LOGGER.info(f"成功设置用户权限: {emby_id}")
                return True
            else:
                LOGGER.error(f"设置用户权限失败: {emby_id} - {result.error}")
                return False
                
        except Exception as e:
            LOGGER.error(f"设置用户权限异常: {emby_id} - {str(e)}")
            return False

    async def get_emby_libs(self) -> Optional[Dict[str, str]]:
        """
        获取所有媒体库
        :return: 媒体库字典 {guid: name}
        """
        try:
            result = await self._request('GET', '/emby/Library/VirtualFolders')
            if result.success and result.data:
                # {guid: lib_name, ...}
                libs = {lib['Guid']: lib['Name'] for lib in result.data}
                LOGGER.debug(f"获取媒体库成功: {libs}")
                return libs
            else:
                LOGGER.error(f"获取媒体库失败: {result.error}")
                return None
        except Exception as e:
            LOGGER.error(f"获取媒体库异常: {str(e)}")
            return None

    async def get_folder_ids_by_names(self, folder_names: List[str]) -> List[str]:
        """
        根据媒体库名称获取对应的ID列表
        :param folder_names: 媒体库名称列表
        :return: 媒体库ID列表
        """
        try:
            result = await self._request('GET', '/emby/Library/VirtualFolders')
            if result.success and result.data:
                folder_ids = []
                for lib in result.data:
                    if lib.get('Name') in folder_names:
                        if lib.get('Guid') is not None:
                            folder_ids.append(lib.get('Guid'))
                LOGGER.debug(f"获取文件夹ID成功: {folder_names} -> {folder_ids}")
                return folder_ids
            else:
                LOGGER.error(f"获取文件夹ID失败: {result.error}")
                return []
        except Exception as e:
            LOGGER.error(f"获取文件夹ID异常: {str(e)}")
            return []

    async def update_user_enabled_folder(self, emby_id: str, enabled_folder_ids: List[str] = None, blocked_media_folders: List[str] = None, 
                                enable_all_folders: bool = True, current_policy: Optional[Dict[str, Any]] = None) -> bool:
        """
        更新用户策略 - 新版本API方法
        :param emby_id: 用户ID
        :param enabled_folder_ids: 启用的文件夹ID列表
        :param blocked_media_folders: 阻止的媒体库名称列表
        :param enable_all_folders: 是否启用所有文件夹
        :param current_policy: 调用方**已经读到**的当前用户策略（可选）。
            传入时不再单独 `GET /Users/{id}` 去读一次（去掉同一账号内的重复读）；
            为 None（默认）时保持原行为，自己读一次。
            加这个参数**有且只有一个目的**就是去重复读，所以现有调用点一律不用改，
            行为逐字不变。
        :return: 是否成功
        """
        try:
            if current_policy is not None:
                # 调用方已提供策略：直接用，省掉一次 GET /emby/Users/{id}
                current_policy = current_policy if isinstance(current_policy, dict) else {}
            else:
                # 首先获取当前用户策略
                user_result = await self._request('GET', f'/emby/Users/{emby_id}')
                if not user_result.success:
                    LOGGER.error(f"获取用户信息失败: {emby_id} - {user_result.error}")
                    return False
                
                current_policy = user_result.data.get('Policy', {})
            
            # 更新策略中的文件夹访问设置
            updated_policy = current_policy.copy()
            updated_policy['EnableAllFolders'] = enable_all_folders
            if blocked_media_folders is not None:
                updated_policy['BlockedMediaFolders'] = blocked_media_folders
            
            if enabled_folder_ids is not None:
                updated_policy['EnabledFolders'] = enabled_folder_ids
            
            # 发送更新请求
            result = await self._request('POST', f'/emby/Users/{emby_id}/Policy', json=updated_policy)
            if result.success:
                LOGGER.info(f"成功更新用户策略: {emby_id} - EnableAllFolders: {enable_all_folders} - EnabledFolders: {enabled_folder_ids}")
                return True
            else:
                LOGGER.error(f"更新用户策略失败: {emby_id} - {result.error}")
                return False
                
        except Exception as e:
            LOGGER.error(f"更新用户策略异常: {emby_id} - {str(e)}")
            return False

    async def get_current_enabled_folder_ids(self, emby_id: str) -> Tuple[List[str], bool, List[str]]:
        """
        获取当前启用的文件夹ID列表（处理 EnableAllFolders 的情况）
        :param emby_id: 用户ID
        :return: (启用的文件夹ID列表, 是否启用所有文件夹, 阻止的媒体库名称列表)
        """
        try:
            success, rep = await self.user(emby_id=emby_id)
            if not success:
                LOGGER.error(f"获取用户信息失败: {emby_id}")
                return [], False, []
            
            policy = rep.get("Policy", {})
            enable_all_folders = policy.get("EnableAllFolders", False)
            blocked_media_folders = policy.get("BlockedMediaFolders", [])
            
            if enable_all_folders is True:
                # 如果启用所有文件夹，需要获取所有媒体库的文件夹ID
                all_libs = await self.get_emby_libs()
                all_folder_ids = list(all_libs.keys()) if all_libs else []
                return all_folder_ids, True, blocked_media_folders
            else:
                current_enabled_folders = policy.get("EnabledFolders", [])
                return current_enabled_folders, False, blocked_media_folders
                
        except Exception as e:
            LOGGER.error(f"获取当前启用文件夹ID异常: {emby_id} - {str(e)}")
            return [], False, []

    async def hide_folders_by_names(self, emby_id: str, folder_names: List[str]) -> bool:
        """
        根据媒体库名称隐藏指定的媒体库
        :param emby_id: 用户ID
        :param folder_names: 要隐藏的媒体库名称列表
        :return: 是否成功
        """
        try:
            # 获取当前启用的文件夹ID列表
            current_enabled_folders, enable_all_folders, blocked_media_folders = await self.get_current_enabled_folder_ids(emby_id)
            
            # 获取要隐藏的媒体库对应的文件夹ID
            hide_folder_ids = await self.get_folder_ids_by_names(folder_names)
            
            if not hide_folder_ids:
                LOGGER.warning(f"未找到要隐藏的媒体库: {folder_names}")
                return True  # 如果找不到，认为操作成功（可能已经隐藏了）
            
            # 从启用列表中移除要隐藏的文件夹ID
            new_enabled_folders = [folder_id for folder_id in current_enabled_folders 
                                  if folder_id not in hide_folder_ids]
            # 将媒体库名称添加到阻止列表中（去重）
            new_blocked_folders = list(set(blocked_media_folders + folder_names)) if blocked_media_folders else folder_names
            # 更新用户策略
            return await self.update_user_enabled_folder(
                emby_id=emby_id,
                enabled_folder_ids=new_enabled_folders,
                blocked_media_folders=new_blocked_folders,
                enable_all_folders=False
            )
            
        except Exception as e:
            LOGGER.error(f"隐藏媒体库异常: {emby_id} - {str(e)}")
            return False

    async def show_folders_by_names(self, emby_id: str, folder_names: List[str]) -> bool:
        """
        根据媒体库名称显示指定的媒体库
        :param emby_id: 用户ID
        :param folder_names: 要显示的媒体库名称列表
        :return: 是否成功
        """
        try:
            # 获取当前启用的文件夹ID列表
            current_enabled_folders, enable_all_folders, blocked_media_folders = await self.get_current_enabled_folder_ids(emby_id)
            
            # 如果已经启用所有文件夹，则不需要修改
            if enable_all_folders is True:
                return await self.update_user_enabled_folder(
                    emby_id=emby_id,
                    blocked_media_folders=[],
                    enable_all_folders=True,
                )
            
            # 获取要显示的媒体库对应的文件夹ID
            show_folder_ids = await self.get_folder_ids_by_names(folder_names)
            
            if not show_folder_ids:
                LOGGER.warning(f"未找到要显示的媒体库: {folder_names}")
                return True  # 如果找不到，认为操作成功
            
            # 将文件夹ID添加到启用列表中（去重）
            new_enabled_folders = list(set(current_enabled_folders + show_folder_ids))
            new_blocked_folders = [name for name in blocked_media_folders if name not in folder_names] if blocked_media_folders else []
            
            # 更新用户策略
            return await self.update_user_enabled_folder(
                emby_id=emby_id,
                enabled_folder_ids=new_enabled_folders,
                blocked_media_folders=new_blocked_folders,
                enable_all_folders=False
            )
            
        except Exception as e:
            LOGGER.error(f"显示媒体库异常: {emby_id} - {str(e)}")
            return False

    async def enable_all_folders_for_user(self, emby_id: str) -> bool:
        """
        启用所有媒体库
        :param emby_id: 用户ID
        :return: 是否成功
        """
        all_libs = await self.get_emby_libs()
        all_lib_guids = list(all_libs.keys()) if all_libs else []
        return await self.update_user_enabled_folder(
            emby_id=emby_id,
            enable_all_folders=True,
            enabled_folder_ids=all_lib_guids,
            blocked_media_folders=[]
        )

    async def disable_all_folders_for_user(self, emby_id: str) -> bool:
        """
        禁用所有媒体库（关闭所有媒体库访问）
        :param emby_id: 用户ID
        :return: 是否成功
        """
        all_libs = await self.get_emby_libs()
        # B-L11：取不到媒体库列表时必须中止，否则会写入空列表=把所有库都禁掉
        if not all_libs:
            LOGGER.error(f"获取媒体库列表失败，已中止禁用媒体库操作: {emby_id}")
            return False
        all_lib_names = list(all_libs.values())
        return await self.update_user_enabled_folder(
            emby_id=emby_id,
            enabled_folder_ids=[],
            blocked_media_folders=all_lib_names,
            enable_all_folders=False
        )

    @cache.memoize(ttl=120)
    async def get_current_playing_count(self) -> int:
        """
        获取当前播放用户数量
        :return: 播放用户数量
        """
        try:
            # 会话端点统一走 register_throttle.sessions_endpoint()（concurrent_play_monitor
            # / watching 等其它 3 处会话调用点同源，窗口用 register_session_active_seconds
            # 配，0 = 退回裸端点）。裸调实测 2,970,545 B / 4037 条 / 7.70s，带
            # `?ActiveWithinSeconds=300` 后 89,940 B / 75 条 / 0.66s；下面本来就只统计
            # NowPlayingItem 非空的会话，僵尸会话纯属白流量，语义等价。
            # 限流模块缺失/导入失败时退化成硬编码端点（`_fallback_sessions_endpoint`），
            # 绝不因为一个优化参数把在线人数统计打断。
            try:
                from bot.func_helper.register_throttle import sessions_endpoint
                endpoint = sessions_endpoint()
            except Exception as e:
                LOGGER.debug(f"sessions_endpoint 不可用，使用兜底端点: {type(e).__name__}: {e}")
                endpoint = _fallback_sessions_endpoint()
            result = await self._request('GET', endpoint)
            if result.success and result.data:
                count = 0
                for session in result.data:
                    if session.get("NowPlayingItem"):
                        count += 1
                LOGGER.debug(f"当前播放用户数: {count}")
                return count
            else:
                LOGGER.error(f"获取播放数量失败: {result.error}")
                return -1
        except Exception as e:
            LOGGER.error(f"获取播放数量异常: {str(e)}")
            return -1

    async def terminate_session(self, session_id: str, reason: str = "Unauthorized client detected") -> bool:
        """
        向指定会话**下发**停止播放指令。

        ⚠️ 返回值的语义是「服务端是否**受理**了这条指令」，**不是**「客户端已经断流」。
        调用方不得把它当作「已终止」对外播报，详见下面 2026-10-06 的实测说明。

        :param session_id: 会话ID
        :param reason: 终止原因
        :return: 服务端是否受理了停止指令（不代表客户端已停止播放）

        ── 2026-10-06 在 ChaPanda（Emby 4.10.0.40）上的实测结论 ──
        `POST /Sessions/{Id}/Playing/Stop` 本质是**通过会话的远程控制通道向客户端
        下发一条 playstate 指令**，不是服务端杀流。Emby 官方文档写明这类命令
        "just assumed supported if SupportsRemoteControl is true"。而该服务器
        3643 个会话里 `SupportsRemoteControl=True` 的 **0 个**；更硬的证据是
        `GET /Sessions?ControllableByUserId=<userId>` 对**管理员自己**也返回 0 条，
        即服务器自己就认为这些会话不可控。

        实测：对正在播放的会话调用本接口，HTTP **204**，但 +5s/+15s/+30s 复查
        `NowPlayingItem` **仍在播放**；`Playing/Pause` 同样无效。

        所以在本服务器的客户端群体下，本接口**必然无效**。它保留的价值是：
        对将来某个真正支持远程控制的客户端仍然有效，且调用它没有副作用。
        真正能停流的手段见 `set_user_disabled()`。
        """
        try:
            LOGGER.info(f"下发停止指令: {session_id} - {reason}")

            # 停止播放
            stop_result = await self._request('POST', f'/emby/Sessions/{session_id}/Playing/Stop')

            # 发送消息给客户端（纯提示，不具备停流能力）
            message_data = {
                "Text": f"🚫 服务端已要求本会话停止播放: {reason}",
                "Header": "安全警告",
                "TimeoutMs": 10000
            }
            await self._request('POST', f'/emby/Sessions/{session_id}/Message', json=message_data)

            # 判据只看 Stop 是否被受理：Message 只是弹窗，成功了也不代表流会停。
            # （旧实现写的是 `stop_result.success or message_result.success`，
            #   只要弹窗发出去就对外宣称"已终止"，是纯粹的谎报。）
            if stop_result.success:
                LOGGER.info(f"停止指令已被服务端受理（不代表客户端已断流）: {session_id}")
                return True

            LOGGER.error(f"停止指令被服务端拒绝: {session_id} - {stop_result.error}")
            return False

        except Exception as e:
            LOGGER.error(f"下发停止指令异常: {session_id} - {str(e)}")
            return False

    async def get_user(self, emby_id: str) -> Optional[dict]:
        """
        读取单个 Emby 用户的完整对象（含 `Policy`）。

        :param emby_id: Emby 用户ID
        :return: 用户 dict；失败返回 None
        """
        try:
            result = await self._request('GET', f'/emby/Users/{emby_id}')
            if result.success and isinstance(result.data, dict):
                return result.data
            LOGGER.warning(f"读取 Emby 用户失败: {emby_id} - {getattr(result, 'error', None)}")
            return None
        except Exception as e:
            LOGGER.error(f"读取 Emby 用户异常: {emby_id} - {type(e).__name__}: {e}")
            return None

    async def is_user_disabled(self, emby_id: str) -> Optional[bool]:
        """
        读取用户当前是否被禁用。

        :return: True/False；**读不到时返回 None**（调用方必须区分「未禁用」与
                 「查不到」，不能把查不到当成已还原 —— 那正是"永久封禁"的成因）
        """
        user = await self.get_user(emby_id)
        if not user:
            return None
        policy = user.get("Policy") or {}
        return bool(policy.get("IsDisabled", False))

    async def set_user_disabled(self, emby_id: str, disabled: bool) -> bool:
        """
        **最小化**地翻转用户的 `IsDisabled`，其余策略字段原样保留。

        与 `emby_change_policy()` 的区别（这个区别很重要）：
        `emby_change_policy()` 用 `create_policy()` **整份覆盖**策略，只保留
        `EnableAllFolders` / `EnabledFolders` / `BlockedMediaFolders` 三个字段，
        其余（如 `SimultaneousStreamLimit`、`EnableRemoteControlOfOtherUsers`、
        码率限制等）都会被重置成 `create_policy()` 的默认值。
        用它做「临时踢流再还原」会把管理员的个性化设置一并抹掉，所以这里改成
        读→改一个字段→写回。

        :param emby_id: Emby 用户ID
        :param disabled: 目标状态
        :return: 是否写入成功（读不到原策略时**拒绝写入**，避免用不完整策略覆盖）
        """
        try:
            user = await self.get_user(emby_id)
            if not user:
                LOGGER.error(f"无法读取用户策略，拒绝写入 IsDisabled={disabled}: {emby_id}")
                return False

            policy = user.get("Policy")
            if not isinstance(policy, dict) or not policy:
                LOGGER.error(f"用户策略为空，拒绝写入 IsDisabled={disabled}: {emby_id}")
                return False

            if bool(policy.get("IsDisabled", False)) == bool(disabled):
                LOGGER.info(f"用户 IsDisabled 已是 {disabled}，无需写入: {emby_id}")
                return True

            policy["IsDisabled"] = bool(disabled)
            result = await self._request('POST', f'/emby/Users/{emby_id}/Policy', json=policy)
            if result.success:
                LOGGER.info(f"已写入 IsDisabled={disabled}: {emby_id}")
                return True

            LOGGER.error(f"写入 IsDisabled={disabled} 失败: {emby_id} - {result.error}")
            return False
        except Exception as e:
            LOGGER.error(f"写入 IsDisabled 异常: {emby_id} - {type(e).__name__}: {e}")
            return False

    async def emby_change_policy(self, emby_id: str, admin: bool = False, disable: bool = False) -> bool:
        """
        修改用户策略
        :param user_id: 用户ID
        :param admin: 是否为管理员
        :param disable: 是否禁用
        :return: 是否成功
        """
        try:
            current_policy = {}
            user_result = await self._request('GET', f'/emby/Users/{emby_id}')
            if user_result.success:
                current_policy = user_result.data.get("Policy", {}) if user_result.data else {}
            else:
                LOGGER.warning(f"获取用户当前策略失败，将使用默认策略更新: {emby_id} - {user_result.error}")

            policy = create_policy(admin=admin, disable=disable)
            if current_policy:
                policy.update({
                    "EnableAllFolders": current_policy.get("EnableAllFolders", False),
                    "EnabledFolders": current_policy.get("EnabledFolders", []),
                    "BlockedMediaFolders": current_policy.get("BlockedMediaFolders", policy.get("BlockedMediaFolders", [])),
                })

            result = await self._request('POST', f'/emby/Users/{emby_id}/Policy', json=policy)
            if result.success:
                # 【接管声明】任何一次显式的策略写入都意味着"这个用户的禁用状态
                # 由本次调用负责"，所以必须作废可能还挂着的「临时封禁踢流」标记。
                #
                # 为什么放在这里、而不是逐个调用点：全仓库有 15+ 处会把用户置为禁用
                # （/kk 面板、renew、syncs、create、userplays_rank、check_ex、
                # user_info、ban_playlist、client_filter、line_report …）。逐个加清理
                # 必然漏掉某个，而漏掉的后果是：管理员刚封的人，几十秒后被临时踢流的
                # 还原任务**悄悄解开**。这里是所有这些路径的唯一收口点。
                #
                # disable=False（解封）与 admin=True（提权，内部同样写 IsDisabled=False）
                # 也要清：管理员的手动操作永远优先于机器人的临时措施。
                #
                # 注意 `set_user_disabled()` **不**走这里 —— 它必须能改写 IsDisabled
                # 而不清掉自己刚落的标记，否则「临时封禁踢流」当场就自我作废了。
                if sql_update_emby(Emby.embyid == emby_id, kick_until=None):
                    LOGGER.debug(f"已清除该用户的临时封禁踢流标记: {emby_id}")
                LOGGER.info(f"成功修改用户策略: {emby_id}")
                return True
            else:
                LOGGER.error(f"修改用户策略失败: {emby_id} - {result.error}")
                return False
        except Exception as e:
            LOGGER.error(f"修改用户策略异常: {emby_id} - {str(e)}")
            return False

    async def authority_account(self, tg_id: int, username: str, password: str = None) -> Tuple[bool, Union[str, int]]:
        """
        验证账户
        :param tg_id: Telegram用户ID
        :param username: 用户名
        :param password: 密码
        :return: (是否成功, 用户ID或错误码)
        """
        try:
            data = {"Username": username}
            if password and password != 'None':
                data["Pw"] = password
                
            result = await self._request('POST', '/emby/Users/AuthenticateByName', json=data)
            if result.success and result.data:
                emby_id = result.data.get("User", {}).get("Id")
                if emby_id:
                    LOGGER.info(f"账户验证成功: {username} -> {emby_id}")
                    return True, emby_id
                else:
                    LOGGER.error(f"账户验证失败，无法获取用户ID: {username}")
                    return False, 0
            else:
                LOGGER.error(f"账户验证失败: {username} - {result.error}")
                return False, 0
        except Exception as e:
            LOGGER.error(f"账户验证异常: {username} - {str(e)}")
            return False, 0

    async def emby_cust_commit(self, emby_id: str = None, days: int = 7, method: str = None) -> Optional[List[Dict]]:
        """
        执行自定义查询（已修复SQL注入问题）
        :param emby_id: 用户ID
        :param days: 查询天数
        :param method: 查询方法
        :return: 查询结果
        """
        try:
            sub_time = datetime.now(timezone(timedelta(hours=8)))
            start_time = (sub_time - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            end_time = sub_time.strftime("%Y-%m-%d %H:%M:%S")
            
            # 注意：由于Emby API的限制，这里仍然需要拼接SQL
            # 在实际生产环境中，建议在Emby服务器端实现参数化查询
            if method == 'sp':
                final_sql = f"SELECT UserId, SUM(PlayDuration - PauseDuration) AS WatchTime FROM PlaybackActivity WHERE DateCreated >= '{start_time}' AND DateCreated < '{end_time}' GROUP BY UserId ORDER BY WatchTime DESC"
            else:
                # B-M2：emby_id 会拼进 SQL，复用 get_emby_report 的白名单校验
                if not emby_id or not str(emby_id).replace('-', '').replace('_', '').isalnum():
                    LOGGER.error(f"无效的用户ID格式: {emby_id}")
                    return None
                final_sql = f"SELECT MAX(DateCreated) AS LastLogin, SUM(PlayDuration - PauseDuration) / 60 AS WatchTime FROM PlaybackActivity WHERE UserId = '{emby_id}' AND DateCreated >= '{start_time}' AND DateCreated < '{end_time}' GROUP BY UserId"
            
            data = {
                "CustomQueryString": final_sql,
                "ReplaceUserId": True
            }
            
            result = await self._request('POST', '/emby/user_usage_stats/submit_custom_query', json=data)
            if result.success and result.data:
                return result.data.get("results", [])
            else:
                LOGGER.error(f"自定义查询失败: {result.error}")
                return None
                
        except Exception as e:
            LOGGER.error(f"自定义查询异常: {str(e)}")
            return None

    async def users(self) -> Tuple[bool, Union[List[Dict], Dict[str, str]]]:
        """
        获取所有用户列表
        :return: (是否成功, 用户列表或错误信息)
        """
        try:
            result = await self._request('GET', '/emby/Users')
            if result.success:
                LOGGER.debug(f"获取用户列表成功，共 {len(result.data)} 个用户")
                return True, result.data
            else:
                LOGGER.error(f"获取用户列表失败: {result.error}")
                return False, {'error': f"🤕Emby 服务器连接失败: {result.error}"}
        except Exception as e:
            LOGGER.error(f"获取用户列表异常: {str(e)}")
            return False, {'error': str(e)}

    async def user(self, emby_id: str) -> Tuple[bool, Union[Dict, Dict[str, str]]]:
        """
        通过ID获取用户信息
        :param emby_id: 用户ID
        :return: (是否成功, 用户信息或错误信息)
        """
        try:
            result = await self._request('GET', f'/emby/Users/{emby_id}')
            if result.success:
                LOGGER.debug(f"获取用户信息成功: {emby_id}")
                return True, result.data
            else:
                LOGGER.error(f"获取用户信息失败: {emby_id} - {result.error}")
                return False, {'error': f"🤕Emby 服务器连接失败: {result.error}"}
        except Exception as e:
            LOGGER.error(f"获取用户信息异常: {emby_id} - {str(e)}")
            return False, {'error': str(e)}

    async def get_emby_user_by_name(self, emby_name: str) -> Tuple[bool, Union[Dict, Dict[str, str]]]:
        """
        通过用户名获取用户信息
        :param emby_name: 用户名
        :return: (是否成功, 用户信息或错误信息)
        """
        try:
            # B-L6：名字必须 URL 编码，且不再把 api_key 拼进 URL（改由 X-Emby-Token 头认证）
            encoded_name = urllib.parse.quote(str(emby_name), safe='')
            result = await self._request('GET', f'/emby/Users/Query?NameStartsWithOrGreater={encoded_name}')
            if result.success and result.data:
                items = result.data.get("Items", [])
                for item in items:
                    if item.get("Name") == emby_name:
                        LOGGER.debug(f"找到用户: {emby_name}")
                        return True, item
                LOGGER.warning(f"未找到用户: {emby_name}")
                return False, {'error': "🤕用户不存在"}
            else:
                LOGGER.error(f"查询用户失败: {emby_name} - {result.error}")
                return False, {'error': f"🤕Emby 服务器连接失败: {result.error}"}
        except Exception as e:
            LOGGER.error(f"查询用户异常: {emby_name} - {str(e)}")
            return False, {'error': str(e)}

    async def add_favorite_items(self, emby_id: str, item_id: str) -> bool:
        """
        添加收藏项目
        :param emby_id: 用户ID
        :param item_id: 项目ID
        :return: 是否成功
        """
        try:
            result = await self._request('POST', f'/emby/Users/{emby_id}/FavoriteItems/{item_id}')
            if result.success:
                LOGGER.info(f"添加收藏成功: {emby_id} -> {item_id}")
                return True
            else:
                LOGGER.error(f"添加收藏失败: {emby_id} -> {item_id} - {result.error}")
                return False
        except Exception as e:
            LOGGER.error(f"添加收藏异常: {emby_id} -> {item_id} - {str(e)}")
            return False

    async def get_favorite_items(self, emby_id: str, start_index: int = None, limit: int = None) -> Union[Dict, bool]:
        """
        获取用户收藏项目
        :param emby_id: 用户ID
        :param start_index: 开始索引
        :param limit: 限制数量
        :return: 收藏项目数据或False
        """
        try:
            url = f"/emby/Users/{emby_id}/Items?Filters=IsFavorite&Recursive=true&IncludeItemTypes=Movie,Series,Episode,Person"
            if start_index is not None:
                url += f"&StartIndex={start_index}"
            if limit is not None:
                url += f"&Limit={limit}"
                
            result = await self._request('GET', url)
            if result.success:
                LOGGER.debug(f"获取收藏成功: {emby_id}")
                return result.data
            else:
                LOGGER.error(f"获取收藏失败: {emby_id} - {result.error}")
                return False
        except Exception as e:
            LOGGER.error(f"获取收藏异常: {emby_id} - {str(e)}")
            return False

    async def item_id_name(self, emby_id: str, item_id: str) -> str:
        """
        通过项目ID获取名称
        :param emby_id: 用户ID
        :param item_id: 项目ID
        :return: 项目名称
        """
        try:
            result = await self._request('GET', f'/emby/Users/{emby_id}/Items/{item_id}')
            if result.success and result.data:
                title = result.data.get("Name", "")
                LOGGER.debug(f"获取项目名称成功: {item_id} -> {title}")
                return title
            else:
                LOGGER.error(f"获取项目名称失败: {item_id} - {result.error}")
                return ""
        except Exception as e:
            LOGGER.error(f"获取项目名称异常: {item_id} - {str(e)}")
            return ""

    async def item_id_people(self, item_id: str) -> Tuple[bool, Union[List[Dict], Dict[str, str]]]:
        """
        获取项目演员信息
        :param item_id: 项目ID
        :return: (是否成功, 演员列表或错误信息)
        """
        try:
            result = await self._request('GET', f'/emby/Items?Ids={item_id}&Fields=People')
            if result.success and result.data:
                items = result.data.get("Items", [])
                if items:
                    people = items[0].get("People", [])
                    LOGGER.debug(f"获取演员信息成功: {item_id}")
                    return True, people
                else:
                    LOGGER.warning(f"项目无演员信息: {item_id}")
                    return False, {'error': "🤕Emby 服务器返回数据为空!"}
            else:
                LOGGER.error(f"获取演员信息失败: {item_id} - {result.error}")
                return False, {'error': f"🤕Emby 服务器连接失败: {result.error}"}
        except Exception as e:
            LOGGER.error(f"获取演员信息异常: {item_id} - {str(e)}")
            return False, {'error': str(e)}

    async def primary(self, item_id: str, width: int = 200, height: int = 300, quality: int = 90) -> Tuple[bool, Union[bytes, Dict[str, str]]]:
        """
        获取主要图片
        :param item_id: 项目ID
        :param width: 宽度
        :param height: 高度
        :param quality: 质量
        :return: (是否成功, 图片数据或错误信息)
        """
        try:
            url = f'/emby/Items/{item_id}/Images/Primary?maxHeight={height}&maxWidth={width}&quality={quality}'
            result = await self._request('GET', url)
            if result.success:
                LOGGER.debug(f"获取主要图片成功: {item_id}")
                return True, result.data
            else:
                LOGGER.error(f"获取主要图片失败: {item_id} - {result.error}")
                return False, {'error': f"🤕Emby 服务器连接失败: {result.error}"}
        except Exception as e:
            LOGGER.error(f"获取主要图片异常: {item_id} - {str(e)}")
            return False, {'error': str(e)}

    async def backdrop(self, item_id: str, width: int = 300, quality: int = 90) -> Tuple[bool, Union[bytes, Dict[str, str]]]:
        """
        获取背景图片
        :param item_id: 项目ID
        :param width: 宽度
        :param quality: 质量
        :return: (是否成功, 图片数据或错误信息)
        """
        try:
            url = f'/emby/Items/{item_id}/Images/Backdrop?maxWidth={width}&quality={quality}'
            result = await self._request('GET', url)
            if result.success:
                LOGGER.debug(f"获取背景图片成功: {item_id}")
                return True, result.data
            else:
                LOGGER.error(f"获取背景图片失败: {item_id} - {result.error}")
                return False, {'error': f"🤕Emby 服务器连接失败: {result.error}"}
        except Exception as e:
            LOGGER.error(f"获取背景图片异常: {item_id} - {str(e)}")
            return False, {'error': str(e)}

    async def items(self, emby_id: str, item_id: str) -> Tuple[bool, Union[Dict, Dict[str, str]]]:
        """
        获取用户的特定项目信息
        :param emby_id: 用户ID
        :param item_id: 项目ID
        :return: (是否成功, 项目信息或错误信息)
        """
        try:
            result = await self._request('GET', f'/emby/Users/{emby_id}/Items/{item_id}')
            if result.success:
                LOGGER.debug(f"获取项目信息成功: {emby_id} -> {item_id}")
                return True, result.data
            else:
                LOGGER.error(f"获取项目信息失败: {emby_id} -> {item_id} - {result.error}")
                return False, {'error': f"🤕Emby 服务器连接失败: {result.error}"}
        except Exception as e:
            LOGGER.error(f"获取项目信息异常: {emby_id} -> {item_id} - {str(e)}")
            return False, {'error': str(e)}

    async def get_emby_report(self, types: str = 'Movie', emby_id: str = None, days: int = 7, 
                             end_date: datetime = None, limit: int = 10) -> Tuple[bool, Union[List[Dict], str]]:
        """
        获取播放报告（已修复SQL注入问题）
        :param types: 类型
        :param emby_id: 用户ID
        :param days: 天数
        :param end_date: 结束日期
        :param limit: 限制数量
        :return: (是否成功, 报告数据或错误信息)
        """
        try:
            if not end_date:
                end_date = datetime.now(timezone(timedelta(hours=8)))

            # B-M2：types 直接拼进 SQL，必须先做白名单校验
            if types not in Embyservice._ALLOWED_ITEM_TYPES:
                LOGGER.error(f"无效的媒体类型: {types}")
                return False, "无效的媒体类型"
            
            start_time = (end_date - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            end_time = end_date.strftime('%Y-%m-%d %H:%M:%S')
            
            # 构建安全的SQL查询
            sql_parts = [
                "SELECT UserId, ItemId, ItemType,",
                " substr(ItemName,0, instr(ItemName, ' - ')) AS name," if types == 'Episode' else "ItemName AS name,",
                "COUNT(1) AS play_count,",
                "SUM(PlayDuration - PauseDuration) AS total_duarion",
                "FROM PlaybackActivity",
                f"WHERE ItemType = '{types}'",  # 这里应该验证types参数
                f"AND DateCreated >= '{start_time}' AND DateCreated <= '{end_time}'",
                "AND UserId not IN (select UserId from UserList)"
            ]
            
            if emby_id:
                # 验证user_id格式，防止SQL注入
                if not emby_id.replace('-', '').replace('_', '').isalnum():
                    LOGGER.error(f"无效的用户ID格式: {emby_id}")
                    return False, "无效的用户ID格式"
                sql_parts.append(f"AND UserId = '{emby_id}'")
            
            sql_parts.extend([
                "GROUP BY name",
                "ORDER BY total_duarion DESC",
                f"LIMIT {int(limit)}"  # 确保limit是整数
            ])
            
            sql = " ".join(sql_parts)
            data = {
                "CustomQueryString": sql,
                "ReplaceUserId": False
            }
            
            result = await self._request('POST', '/emby/user_usage_stats/submit_custom_query', json=data)
            if result.success and result.data:
                ret = result.data
                if len(ret.get("colums", [])) == 0:
                    return False, ret.get("message", "无数据")
                LOGGER.debug(f"获取播放报告成功: {types}")
                return True, ret.get("results", [])
            else:
                LOGGER.error(f"获取播放报告失败: {result.error}")
                return False, f"🤕Emby 服务器连接失败: {result.error}"
                
        except Exception as e:
            LOGGER.error(f"获取播放报告异常: {str(e)}")
            return False, str(e)

    async def get_emby_userip(self, emby_id: str) -> Tuple[bool, Union[List[Dict], str]]:
        """
        获取用户IP和设备信息（已修复SQL注入问题）
        :param emby_id: 用户ID
        :return: (是否成功, 设备信息或错误信息)
        """
        try:
            # 验证user_id格式
            if not emby_id.replace('-', '').replace('_', '').isalnum():
                LOGGER.error(f"无效的用户ID格式: {emby_id}")
                return False, "无效的用户ID格式"
            
            sql = f"SELECT DeviceName,ClientName, RemoteAddress FROM PlaybackActivity WHERE UserId = '{emby_id}'"
            data = {
                "CustomQueryString": sql,
                "ReplaceUserId": True
            }
            
            result = await self._request('POST', '/emby/user_usage_stats/submit_custom_query', json=data)
            if result.success and result.data:
                ret = result.data
                if len(ret.get("colums", [])) == 0:
                    return False, ret.get("message", "无数据")
                LOGGER.debug(f"获取用户设备信息成功: {emby_id}")
                return True, ret.get("results", [])
            else:
                LOGGER.error(f"获取用户设备信息失败: {emby_id} - {result.error}")
                return False, f"🤕Emby 服务器连接失败: {result.error}"
                
        except Exception as e:
            LOGGER.error(f"获取用户设备信息异常: {emby_id} - {str(e)}")
            return False, str(e)

    async def get_users_by_ip(self, ip_address: str, days: int = None) -> Tuple[bool, Union[List[Dict], str]]:
        """
        根据IP地址查询使用该IP的用户信息（已修复SQL注入问题）
        :param ip_address: IP地址
        :param days: 查询天数范围，默认30天
        :return: (是否成功, 用户信息列表或错误信息)
        """
        try:
            # 验证IP地址格式（简单验证）
            import re
            ip_pattern = r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
            if not re.match(ip_pattern, ip_address):
                LOGGER.error(f"无效的IP地址格式: {ip_address}")
                return False, "无效的IP地址格式"
            
            
            
            # 构建安全的SQL查询，查询使用指定IP的用户
            sql = f"""
                SELECT DISTINCT UserId, 
                       DeviceName, 
                       ClientName, 
                       RemoteAddress,
                       MAX(DateCreated) AS LastActivity,
                       COUNT(*) AS ActivityCount
                FROM PlaybackActivity 
                WHERE RemoteAddress = '{ip_address}' 
                
            """
            if days:
                # 计算查询时间范围
                sub_time = datetime.now(timezone(timedelta(hours=8)))
                start_time = (sub_time - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                end_time = sub_time.strftime("%Y-%m-%d %H:%M:%S")
                sql += f" AND DateCreated >= '{start_time}' AND DateCreated <= '{end_time}'"
            sql += " GROUP BY UserId, DeviceName, ClientName, RemoteAddress"
            sql += " ORDER BY LastActivity DESC"
            
            data = {
                "CustomQueryString": sql,
                "ReplaceUserId": False
            }
            
            result = await self._request('POST', '/emby/user_usage_stats/submit_custom_query', json=data)
            if result.success and result.data:
                ret = result.data
                if len(ret.get("colums", [])) == 0:
                    return False, ret.get("message", "无数据")
                
                # 获取查询结果
                results = ret.get("results", [])
                
                # 为每个用户获取用户名信息
                enriched_results = []
                for result_item in results:
                    # B-M12：与设备名/客户端名查询保持一致的长度兜底，避免索引越界
                    if not result_item or len(result_item) < 4:
                        LOGGER.warning(f"跳过异常的结果行: {result_item}")
                        continue
                    user_id = result_item[0]  # UserId 是第一列
                    
                    # 获取用户详细信息
                    user_success, user_info = await self.user(user_id)
                    username = "未知用户"
                    if user_success and isinstance(user_info, dict):
                        username = user_info.get("Name", "未知用户")
                    
                    enriched_item = {
                        "UserId": user_id,
                        "Username": username,
                        "DeviceName": result_item[1],
                        "ClientName": result_item[2], 
                        "RemoteAddress": result_item[3],
                        "LastActivity": result_item[4] if len(result_item) > 4 else "未知",
                        "ActivityCount": result_item[5] if len(result_item) > 5 else 0
                    }
                    enriched_results.append(enriched_item)
                
                LOGGER.info(f"根据IP查询用户成功: {ip_address} - 找到 {len(enriched_results)} 个用户")
                return True, enriched_results
            else:
                LOGGER.error(f"根据IP查询用户失败: {ip_address} - {result.error}")
                return False, f"🤕Emby 服务器连接失败: {result.error}"
                
        except Exception as e:
            LOGGER.error(f"根据IP查询用户异常: {ip_address} - {str(e)}")
            return False, str(e)

    async def get_users_by_device_name(self, device_name: str, days: int = None) -> Tuple[bool, Union[List[Dict], str]]:
        """
        根据设备名关键词查询使用该设备的用户信息（已修复SQL注入问题）
        :param device_name: 设备名关键词
        :param days: 查询天数范围，None表示查询所有时间
        :return: (是否成功, 用户信息列表或错误信息)
        """
        try:
            # 验证关键词（基本的安全检查）
            if not device_name or len(device_name.strip()) == 0:
                LOGGER.error("设备名关键词不能为空")
                return False, "设备名关键词不能为空"
            
            # B-M3：白名单过滤关键词（含 LIKE 元字符 % _），避免注入与全表匹配
            safe_keyword = self._sanitize_like_keyword(device_name)
            if not safe_keyword:
                LOGGER.error(f"设备名关键词包含无效字符: {device_name}")
                return False, "设备名关键词包含无效字符"
            
            # 构建安全的SQL查询，查询使用包含指定关键词的设备名的用户
            sql = f"""
                SELECT DISTINCT UserId, 
                       DeviceName, 
                       ClientName, 
                       RemoteAddress,
                       MAX(DateCreated) AS LastActivity,
                       COUNT(*) AS ActivityCount
                FROM PlaybackActivity 
                WHERE DeviceName LIKE '%{safe_keyword}%' 
            """
            if days:
                # 计算查询时间范围
                sub_time = datetime.now(timezone(timedelta(hours=8)))
                start_time = (sub_time - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                end_time = sub_time.strftime("%Y-%m-%d %H:%M:%S")
                sql += f" AND DateCreated >= '{start_time}' AND DateCreated <= '{end_time}'"
            sql += " GROUP BY UserId, DeviceName, ClientName, RemoteAddress"
            sql += " ORDER BY LastActivity DESC"
            
            data = {
                "CustomQueryString": sql,
                "ReplaceUserId": False
            }
            
            result = await self._request('POST', '/emby/user_usage_stats/submit_custom_query', json=data)
            if result.success and result.data:
                ret = result.data
                if len(ret.get("colums", [])) == 0:
                    return False, ret.get("message", "无数据")
                
                # 获取查询结果
                results = ret.get("results", [])
                
                # 为每个用户获取用户名信息
                enriched_results = []
                for result_item in results:
                    user_id = result_item[0]  # UserId 是第一列
                    
                    # 获取用户详细信息
                    user_success, user_info = await self.user(user_id)
                    username = "未知用户"
                    if user_success and isinstance(user_info, dict):
                        username = user_info.get("Name", "未知用户")
                    
                    enriched_item = {
                        "UserId": user_id,
                        "Username": username,
                        "DeviceName": result_item[1],
                        "ClientName": result_item[2], 
                        "RemoteAddress": result_item[3],
                        "LastActivity": result_item[4] if len(result_item) > 4 else "未知",
                        "ActivityCount": result_item[5] if len(result_item) > 5 else 0
                    }
                    enriched_results.append(enriched_item)
                
                LOGGER.info(f"根据设备名查询用户成功: {device_name} - 找到 {len(enriched_results)} 个用户")
                return True, enriched_results
            else:
                LOGGER.error(f"根据设备名查询用户失败: {device_name} - {result.error}")
                return False, f"🤕Emby 服务器连接失败: {result.error}"
                
        except Exception as e:
            LOGGER.error(f"根据设备名查询用户异常: {device_name} - {str(e)}")
            return False, str(e)

    async def get_users_by_client_name(self, client_name: str, days: int = None) -> Tuple[bool, Union[List[Dict], str]]:
        """
        根据客户端名关键词查询使用该客户端的用户信息（已修复SQL注入问题）
        :param client_name: 客户端名关键词
        :param days: 查询天数范围，None表示查询所有时间
        :return: (是否成功, 用户信息列表或错误信息)
        """
        try:
            # 验证关键词（基本的安全检查）
            if not client_name or len(client_name.strip()) == 0:
                LOGGER.error("客户端名关键词不能为空")
                return False, "客户端名关键词不能为空"
            
            # B-M3：白名单过滤关键词（含 LIKE 元字符 % _），避免注入与全表匹配
            safe_keyword = self._sanitize_like_keyword(client_name)
            if not safe_keyword:
                LOGGER.error(f"客户端名关键词包含无效字符: {client_name}")
                return False, "客户端名关键词包含无效字符"
            
            # 构建安全的SQL查询，查询使用包含指定关键词的客户端名的用户
            sql = f"""
                SELECT DISTINCT UserId, 
                       DeviceName, 
                       ClientName, 
                       RemoteAddress,
                       MAX(DateCreated) AS LastActivity,
                       COUNT(*) AS ActivityCount
                FROM PlaybackActivity 
                WHERE ClientName LIKE '%{safe_keyword}%' 
            """
            if days:
                # 计算查询时间范围
                sub_time = datetime.now(timezone(timedelta(hours=8)))
                start_time = (sub_time - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                end_time = sub_time.strftime("%Y-%m-%d %H:%M:%S")
                sql += f" AND DateCreated >= '{start_time}' AND DateCreated <= '{end_time}'"
            sql += " GROUP BY UserId, DeviceName, ClientName, RemoteAddress"
            sql += " ORDER BY LastActivity DESC"
            
            data = {
                "CustomQueryString": sql,
                "ReplaceUserId": False
            }
            
            result = await self._request('POST', '/emby/user_usage_stats/submit_custom_query', json=data)
            if result.success and result.data:
                ret = result.data
                if len(ret.get("colums", [])) == 0:
                    return False, ret.get("message", "无数据")
                
                # 获取查询结果
                results = ret.get("results", [])
                
                # 为每个用户获取用户名信息
                enriched_results = []
                for result_item in results:
                    user_id = result_item[0]  # UserId 是第一列
                    
                    # 获取用户详细信息
                    user_success, user_info = await self.user(user_id)
                    username = "未知用户"
                    if user_success and isinstance(user_info, dict):
                        username = user_info.get("Name", "未知用户")
                    
                    enriched_item = {
                        "UserId": user_id,
                        "Username": username,
                        "DeviceName": result_item[1],
                        "ClientName": result_item[2], 
                        "RemoteAddress": result_item[3],
                        "LastActivity": result_item[4] if len(result_item) > 4 else "未知",
                        "ActivityCount": result_item[5] if len(result_item) > 5 else 0
                    }
                    enriched_results.append(enriched_item)
                
                LOGGER.info(f"根据客户端名查询用户成功: {client_name} - 找到 {len(enriched_results)} 个用户")
                return True, enriched_results
            else:
                LOGGER.error(f"根据客户端名查询用户失败: {client_name} - {result.error}")
                return False, f"🤕Emby 服务器连接失败: {result.error}"
                
        except Exception as e:
            LOGGER.error(f"根据客户端名查询用户异常: {client_name} - {str(e)}")
            return False, str(e)

    async def get_emby_user_devices(self, offset: int = 0, limit: int = 20) -> Tuple[bool, List[Dict], bool, bool]:
        """
        获取用户设备统计，支持分页
        :param offset: 偏移量
        :param limit: 每页数量
        :return: (是否成功, 设备数据, 是否有上一页, 是否有下一页)
        """
        try:
            sql = f"""
                SELECT UserId, 
                       COUNT(DISTINCT DeviceName || '' || ClientName) AS device_count,
                       COUNT(DISTINCT RemoteAddress) AS ip_count 
                FROM PlaybackActivity 
                GROUP BY UserId 
                ORDER BY device_count DESC 
                LIMIT {int(limit + 1)} 
                OFFSET {int(offset)}
            """
            
            data = {
                "CustomQueryString": sql,
                "ReplaceUserId": True
            }
            
            result = await self._request('POST', '/emby/user_usage_stats/submit_custom_query', json=data)
            if result.success and result.data:
                ret = result.data
                if len(ret.get("colums", [])) == 0:
                    return False, [], False, False
                
                results = ret.get("results", [])
                
                # 判断是否有下一页
                has_next = len(results) > limit
                if has_next:
                    results = results[:-1]  # 去掉多查的一条
                
                # 判断是否有上一页
                has_prev = offset > 0
                
                LOGGER.debug(f"获取用户设备统计成功: offset={offset}, limit={limit}")
                return True, results, has_prev, has_next
            else:
                LOGGER.error(f"获取用户设备统计失败: {result.error}")
                return False, [], False, False
                
        except Exception as e:
            LOGGER.error(f"获取用户设备统计异常: {str(e)}")
            return False, [], False, False

    async def get_medias_count(self) -> str:
        """
        获取媒体数量统计（B-L4：改为实例方法，复用单例会话与统一的错误处理/重试）
        :return: 统计文本
        """
        try:
            result = await self._request('GET', '/emby/Items/Counts')
            if result.success and result.data:
                data = result.data
                movie_count = data.get("MovieCount", 0)
                tv_count = data.get("SeriesCount", 0)
                episode_count = data.get("EpisodeCount", 0)
                music_count = data.get("SongCount", 0)

                txt = f'🎬 电影数量：{movie_count}\n' \
                      f'📽️ 剧集数量：{tv_count}\n' \
                      f'🎵 音乐数量：{music_count}\n' \
                      f'🎞️ 总集数：{episode_count}\n'
                LOGGER.debug("获取媒体统计成功")
                return txt
            else:
                LOGGER.error(f"获取媒体统计失败: {result.error}")
                return '🤕Emby 服务器返回数据为空!'
        except Exception as e:
            LOGGER.error(f"获取媒体统计异常: {str(e)}")
            return '🤕Emby 服务器连接失败!'

    async def get_movies(self, title: str, start: int = 0, limit: int = 5) -> List[Dict]:
        """
        搜索电影/剧集
        :param title: 标题
        :param start: 开始索引
        :param limit: 限制数量
        :return: 电影/剧集列表
        """
        try:
            # URL编码处理
            import urllib.parse
            encoded_title = urllib.parse.quote(title)
            
            url = (f"/emby/Items?IncludeItemTypes=Movie,Series"
                   f"&Fields=ProductionYear,Overview,OriginalTitle,Taglines,ProviderIds,Genres,RunTimeTicks,ProductionLocations,DateCreated,Studios"
                   f"&StartIndex={int(start)}&Recursive=true&SearchTerm={encoded_title}&Limit={int(limit)}&IncludeSearchTypes=false")
            
            # B-H4：短超时作为本次请求参数传入，不再改写单例共享的 self.timeout
            result = await self._request('GET', url, timeout=aiohttp.ClientTimeout(total=3))
            
            if result.success and result.data:
                items = result.data.get("Items", [])
                ret_movies = []
                
                for item in items:
                    # 处理标题
                    name = item.get("Name", "")
                    original_title = item.get("OriginalTitle", "")
                    display_title = name if name == original_title else f'{name} - {original_title}'
                    
                    # 处理其他字段
                    production_locations = ", ".join(item.get("ProductionLocations", ["普遍"]))
                    genres = ", ".join(item.get("Genres", ["未知"]))
                    runtime = convert_runtime(item.get("RunTimeTicks")) if item.get("RunTimeTicks") else '数据缺失'
                    tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
                    
                    movie_item = {
                        'item_type': item.get("Type"),
                        'item_id': item.get("Id"),
                        'title': display_title,
                        'year': item.get("ProductionYear", '缺失'),
                        'od': production_locations,
                        'genres': genres,
                        'photo': f'{self.url}/emby/Items/{item.get("Id")}/Images/Primary?maxHeight=400&maxWidth=600&quality=90',
                        'runtime': runtime,
                        'overview': item.get("Overview", "暂无更多信息"),
                        'taglines': '简介：' if not item.get("Taglines") else item.get("Taglines")[0],
                        'tmdbid': tmdb_id,
                        'add': item.get("DateCreated", "None.").split('.')[0],
                    }
                    ret_movies.append(movie_item)
                
                LOGGER.debug(f"搜索电影成功: {title} - 找到 {len(ret_movies)} 个结果")
                return ret_movies
            else:
                LOGGER.error(f"搜索电影失败: {title} - {result.error}")
                return []
                
        except Exception as e:
            LOGGER.error(f"搜索电影异常: {title} - {str(e)}")
            return []

    async def get_device_by_deviceid(self, deviceid: str) -> Tuple[bool, Union[Dict, Dict[str, str]]]:
        """
        通过设备ID获取设备信息
        :param deviceid: 设备ID
        :return: (是否成功, 设备信息或错误信息)
        """
        try:
            result = await self._request('GET', f'/emby/Devices/Info?Id={deviceid}')
            if result.success:
                LOGGER.debug(f"获取设备信息成功: {deviceid}")
                return True, result.data
            else:
                LOGGER.error(f"获取设备信息失败: {deviceid} - {result.error}")
                return False, "获取设备信息失败"
        except Exception as e:
            LOGGER.error(f"获取设备信息异常: {deviceid} - {str(e)}")
            return False, '获取设备信息异常'

    def __del__(self):
        """析构函数，确保资源清理（B-L5：不再使用 get_event_loop，尽力而为不抛异常）"""
        try:
            session = getattr(self, '_session', None)
            if not session or session.closed:
                return
            # 3.12+ 无运行中的事件循环时 get_running_loop 抛 RuntimeError，被下面的 except 吞掉
            loop = asyncio.get_running_loop()
            loop.create_task(self.close())
        except Exception:
            pass


# 创建全局实例
emby = Embyservice(emby_url, emby_api)
