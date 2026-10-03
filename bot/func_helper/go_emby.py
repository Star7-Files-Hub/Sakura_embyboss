#! /usr/bin/python3
# -*- coding:utf-8 -*-
"""
go-emby（自研 Go Emby 服务端，sd87671067/go-emby）的 /admin 管理接口客户端。

为什么需要它：
    1 号机对接的 Emby 实为 go-emby（`GET /emby/System/Info/Public` 返回
    `ProductName='Go Emby STRM'`）。它只实现 Emby API 的一个子集：

    - `POST /emby/Users/{id}/Policy` → HTTP 204 但**请求体被丢弃**
      （服务端 main.go 里 `/users/{id}/{sub}` 的 switch 只有 password/views/items，
      没有 case "policy"，请求穿过所有分支落到兜底 204）；
    - `GET /emby/Users/{id}` 的 Policy 是 `userDTO()` 硬编码合成的
      （`RemoteClientBitrateLimit` 恒为 0、`IsDisabled` 恒为 false）；
    - `POST /emby/Sessions/{id}/Playing/Stop`、`/Message`、`DELETE /emby/Sessions/{id}`
      → HTTP 404「未实现此接口」。

    因此在这台服务器上，封禁 / 播放权限 / 设备数限制**全部是空操作**。
    真正生效的是它自研的 `/admin/users`。

鉴权（实测 + 源码确认）：
    `/admin/*` 的守卫是 `if !u.Admin || u.API { 403 "需要管理员账号" }`，
    即 **Emby API Key 会被拒绝**，必须用真实管理员账号登录换 token：
        `POST /emby/Users/AuthenticateByName`  body `{"Username":..,"Pw":..}`
        → 200 `{"AccessToken": "<64位>", "User": {...}, "ServerId": ...}`，token 有效期 **30 天**。

    ⚠️ 登录接口有**按 IP 的限流**：1 分钟内 ≥15 次返回 429「登录过于频繁」
    （源码 main.go `login()`：`if len(recent) >= 15`）。所以 token **必须缓存复用**，
    本模块的 `login()` 默认只登录一次，只有 `force=True` 或收到 401/403 才重登。

安全约定：
    **管理员密码只存在于内存，绝不出现在任何日志、异常消息或返回值里。**
    所有可能落日志的文本都先过 `_sanitize()`，它会把密码字面量替换成 `***`。
"""
import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from bot import config, LOGGER

#: 官方 Emby
SERVER_OFFICIAL = 'official'
#: 自研 go-emby（需要走 /admin 接口）
SERVER_GO_EMBY = 'go_emby'

#: 单次请求超时（登录接口偶尔较慢，给 20s）
_TIMEOUT = aiohttp.ClientTimeout(total=20)

#: MaxDevices 的合法区间（服务端 PUT/POST 都会校验，超出返回 400「设备数量范围 1–100」）
_MAX_DEVICES_MIN = 1
_MAX_DEVICES_MAX = 100

# 探测缓存与单例
_server_type_cache: Optional[str] = None
_admin_singleton: Optional['GoEmbyAdmin'] = None
_admin_warned = False


def _is_go_emby_product(product_name: Optional[str]) -> bool:
    """ProductName 是否是 go-emby（不区分大小写，兼容 'go emby' / 'go-emby'）。"""
    if not product_name:
        return False
    name = str(product_name).strip().lower()
    return 'go emby' in name or 'go-emby' in name


class GoEmbyAdmin:
    """go-emby `/admin` 接口客户端。

    只负责 HTTP 与语义，不持有全局状态；全局单例由 `get_go_emby_admin()` 提供。
    """

    def __init__(self, base_url: str, api_key: str, admin_user: str, admin_password: str):
        self.base_url = (base_url or '').strip().rstrip('/')
        # /admin/* 会拒绝 API Key（u.API → 403），这里仅按冻结签名保留，不用于 /admin 鉴权
        self.api_key = api_key or ''
        self.admin_user = (admin_user or '').strip()
        # 密码只留在内存，任何日志都不打印
        self._admin_password = admin_password or ''
        self._token: Optional[str] = None
        # 并发调用时保证只登录一次（登录接口有 IP 限流）
        self._login_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 工具
    def _sanitize(self, text: Any) -> str:
        """把任意文本里的管理员密码抹掉，确保可以安全落日志。

        ⚠️ 必须替换密码的**多种转义形态**，不能只替换原文。
        密码里若含反斜杠、换行、制表符等字符，一旦被 `repr()` 或 JSON 序列化
        就会变成另一种写法（`\\\\` / `\\n` / `\\t`），此时只 `replace(原文)` 匹配不到，
        密码前缀仍会漏进日志。触发场景实测存在：`Message`/`error` 是 list 或 dict
        时 `str()` 走的就是 repr，密码含一个反斜杠就会泄漏出前 3 个字符。
        因此这里同时替换：原文、JSON 转义形态、Python repr 转义形态。
        """
        s = str(text)
        pw = self._admin_password
        if not pw:
            return s

        variants = {pw}
        try:
            variants.add(json.dumps(pw)[1:-1])   # JSON 转义形态
        except Exception:
            pass
        try:
            variants.add(repr(pw)[1:-1])         # Python repr 转义形态
        except Exception:
            pass
        variants.discard('')

        # 长的先替换：转义形态通常更长，先替换可避免短形态抢先吃掉一部分字符
        for variant in sorted(variants, key=len, reverse=True):
            s = s.replace(variant, '***')
        return s

    def _error_message(self, text: Optional[str]) -> str:
        """从服务端错误体里取出可读消息（形如 {"Message":..,"error":..}）。

        ⚠️ 顺序必须是 **先脱敏、再截断**，不能反过来。
        `_sanitize()` 靠「完整密码字面量」做替换；若先把文本截到 200 字符，而密码
        恰好跨过第 200 个字符，完整密码就已经不在串里了，`replace` 匹配不到，
        会把密码前缀原样漏进日志。实测可复现：密码起始偏移 190 时会泄漏出前
        10 个字符。服务端把 Go panic 堆栈回显进错误体时很容易超过 200 字符。
        """
        if not text:
            return ''
        try:
            data = json.loads(text)
        except Exception:
            return self._sanitize(text)[:200]
        if isinstance(data, dict):
            msg = data.get('Message') or data.get('error') or ''
        else:
            msg = data
        return self._sanitize(msg)[:200]

    def _headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self._token:
            # 服务端 token(r) 优先读 X-Emby-Token
            headers['X-Emby-Token'] = self._token
        return headers

    # ------------------------------------------------------------------ 登录
    async def login(self, force: bool = False) -> Optional[str]:
        """登录取管理员 token 并缓存。

        默认复用已缓存的 token（有效期 30 天）；`force=True` 时强制重登。
        失败返回 None 并 LOGGER.error —— **绝不打印密码**。
        """
        if not force and self._token:
            return self._token

        if not self.admin_user or not self._admin_password:
            LOGGER.error("go-emby 管理员凭据未配置（emby_admin_user/emby_admin_password），无法登录 /admin 接口")
            return None

        async with self._login_lock:
            # 双重检查：并发进入时，先到的那个已经登录好了就直接复用
            if not force and self._token:
                return self._token

            url = f'{self.base_url}/emby/Users/AuthenticateByName'
            payload = {'Username': self.admin_user, 'Pw': self._admin_password}
            try:
                async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                    async with session.post(url, json=payload, ssl=False) as resp:
                        status = resp.status
                        text = await resp.text()
            except Exception as e:
                # 只打异常类型与经过脱敏的消息，绝不带上请求体
                LOGGER.error(f"go-emby 管理员登录请求异常: {type(e).__name__}: {self._sanitize(e)}")
                return None

            if status != 200:
                LOGGER.error(
                    f"go-emby 管理员登录失败: HTTP {status} "
                    f"{self._sanitize(self._error_message(text))}"
                )
                return None

            try:
                data = json.loads(text)
            except Exception as e:
                LOGGER.error(f"go-emby 管理员登录响应解析失败: {type(e).__name__}")
                return None

            token = (data or {}).get('AccessToken') if isinstance(data, dict) else None
            if not token:
                LOGGER.error("go-emby 管理员登录响应里没有 AccessToken")
                return None

            self._token = token
            LOGGER.info(f"go-emby 管理员登录成功（账号 {self.admin_user}），token 已缓存复用")
            return token

    # ------------------------------------------------------------- 底层请求
    async def _request(self, method: str, endpoint: str, json_body: Optional[dict] = None,
                       retry_auth: bool = True) -> Tuple[Optional[int], Optional[str]]:
        """发一次带 token 的请求。

        :return: (status, text)；网络异常返回 (None, None)
        """
        if not await self.login():
            return None, None

        url = f'{self.base_url}{endpoint}'
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.request(method, url, headers=self._headers(),
                                           json=json_body, ssl=False) as resp:
                    return resp.status, await resp.text()
        except Exception as e:
            LOGGER.error(f"go-emby 请求异常 {method} {endpoint}: {type(e).__name__}: {self._sanitize(e)}")
            return None, None

    async def _authed_request(self, method: str, endpoint: str,
                              json_body: Optional[dict] = None) -> Tuple[Optional[int], Optional[str]]:
        """带 401/403 自动重登重试一次（token 过期或被顶）的请求。"""
        status, text = await self._request(method, endpoint, json_body=json_body)
        if status in (401, 403):
            LOGGER.warning(
                f"go-emby {method} {endpoint} 返回 HTTP {status}"
                f"（{self._sanitize(self._error_message(text))}），强制重新登录后重试一次"
            )
            if await self.login(force=True):
                status, text = await self._request(method, endpoint, json_body=json_body, retry_auth=False)
        return status, text

    # ------------------------------------------------------------------ 用户
    async def list_users(self) -> Optional[List[dict]]:
        """`GET /admin/users` → 用户 DTO 数组；失败返回 None。"""
        status, text = await self._authed_request('GET', '/admin/users')
        if status is None:
            # 拿不到 token 时 _request 返回 (None, None)：此时报 "HTTP None" 会误导排障
            LOGGER.error("go-emby 获取用户列表失败：无法取得管理员 token（详见上面的登录错误）")
            return None
        if status != 200:
            LOGGER.error(
                f"go-emby 获取用户列表失败: HTTP {status} "
                f"{self._sanitize(self._error_message(text))}".rstrip()
            )
            return None
        try:
            data = json.loads(text)
        except Exception as e:
            LOGGER.error(f"go-emby 用户列表解析失败: {type(e).__name__}")
            return None
        if not isinstance(data, list):
            LOGGER.error(f"go-emby 用户列表返回了非数组（{type(data).__name__}）")
            return None
        return data

    async def get_user(self, user_id: str) -> Optional[dict]:
        """按 Id 在用户列表里匹配；找不到返回 None。"""
        if not user_id:
            return None
        users = await self.list_users()
        if not users:
            return None
        for user in users:
            if isinstance(user, dict) and user.get('Id') == user_id:
                return user
        return None

    async def set_user(self, user_id: str, *, max_devices: Optional[int] = None,
                       admin: Optional[bool] = None,
                       allow_playback: Optional[bool] = None) -> bool:
        """修改 go-emby 用户。

        ⚠️ 服务端 PUT 的 SQL 是 `UPDATE users SET max_devices=?,admin=? WHERE id=?`
        —— **只传想改的字段不够**，未指定的字段必须回填当前值，否则会被写坏
        （例如只想改 AllowPlayback 却把 Admin 置 false = 把管理员降权）。

        实现顺序：先 get_user 拿当前值 → MaxDevices 夹到 1..100 → PUT。

        :return: 成功 True；任何一步失败 False
        """
        if not user_id:
            LOGGER.error("go-emby 修改用户失败：user_id 为空")
            return False

        current = await self.get_user(user_id)
        if not current:
            LOGGER.error(f"go-emby 修改用户失败：在 /admin/users 里找不到用户 {user_id}")
            return False

        policy = current.get('Policy') if isinstance(current.get('Policy'), dict) else {}
        # DTO 顶层有 MaxDevices（userDTO 末尾 "MaxDevices": u.Max），
        # 老版本可能只给 Policy.SimultaneousStreamLimit，做个兜底
        cur_max = current.get('MaxDevices')
        if cur_max is None:
            cur_max = policy.get('SimultaneousStreamLimit')
        cur_admin = bool(policy.get('IsAdministrator', False))

        # 1) MaxDevices：未指定则回填当前值；指定则夹到 1..100
        if max_devices is None:
            target_max = cur_max
        else:
            try:
                target_max = int(max_devices)
            except (TypeError, ValueError):
                LOGGER.error(f"go-emby 修改用户失败：max_devices={max_devices!r} 不是整数")
                return False
            if not _MAX_DEVICES_MIN <= target_max <= _MAX_DEVICES_MAX:
                clamped = min(_MAX_DEVICES_MAX, max(_MAX_DEVICES_MIN, target_max))
                LOGGER.warning(
                    f"go-emby MaxDevices={target_max} 超出 {_MAX_DEVICES_MIN}..{_MAX_DEVICES_MAX}，"
                    f"已夹到 {clamped}"
                )
                target_max = clamped

        # 拿不到合法当前值就宁可不写：写错会直接改坏该用户的设备数
        if not isinstance(target_max, int) or not _MAX_DEVICES_MIN <= target_max <= _MAX_DEVICES_MAX:
            LOGGER.error(
                f"go-emby 无法确定用户 {user_id} 的合法 MaxDevices（{target_max!r}），"
                f"拒绝写入以免改坏"
            )
            return False

        target_admin = cur_admin if admin is None else bool(admin)

        body: Dict[str, Any] = {
            'ID': user_id,
            'MaxDevices': target_max,
            'Admin': target_admin,
        }
        # AllowPlayback 是 *bool：只有显式传了才带，避免误改播放权限
        if allow_playback is not None:
            body['AllowPlayback'] = bool(allow_playback)

        status, text = await self._authed_request('PUT', '/admin/users', json_body=body)
        if status is None:
            LOGGER.error(f"go-emby 修改用户 {user_id} 失败：无法取得管理员 token（详见上面的登录错误）")
            return False
        if status != 200:
            LOGGER.error(
                f"go-emby 修改用户失败 {user_id}: HTTP {status} "
                f"{self._sanitize(self._error_message(text))}".rstrip()
            )
            return False

        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get('ok') is not True:
            LOGGER.error(f"go-emby 修改用户 {user_id} 返回 200 但没有 ok:true")
            return False

        LOGGER.info(
            f"go-emby 已更新用户 {user_id}: MaxDevices={target_max} Admin={target_admin}"
            + (f" AllowPlayback={bool(allow_playback)}" if allow_playback is not None else '')
        )
        return True


# ====================================================================== 探测
async def detect_server_type(force: bool = False) -> str:
    """探测当前 Emby 是不是 go-emby。

    - `config.emby_server_type` 显式配成 'official'/'go_emby' 时**直接采信、不探测**；
    - 配成 'auto'（默认）时才去 `GET /emby/System/Info/Public` 读 ProductName
      （该接口**不需要 token**，实测 200）；
    - 结果缓存；请求失败时返回缓存值，没有缓存则兜底 'official'。
    """
    configured = str(getattr(config, 'emby_server_type', 'auto') or 'auto').strip().lower()
    if configured in (SERVER_OFFICIAL, SERVER_GO_EMBY) and not force:
        return configured

    global _server_type_cache
    if _server_type_cache and not force:
        return _server_type_cache

    base = str(getattr(config, 'emby_url', '') or '').strip().rstrip('/')
    if not base:
        LOGGER.warning("go-emby 探测跳过：config.emby_url 为空")
        return _server_type_cache or SERVER_OFFICIAL

    url = f'{base}/emby/System/Info/Public'
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    LOGGER.warning(f"go-emby 探测失败: HTTP {resp.status}，沿用缓存/兜底 official")
                    return _server_type_cache or SERVER_OFFICIAL
                data = await resp.json(content_type=None)
    except Exception as e:
        LOGGER.warning(f"go-emby 探测请求异常: {type(e).__name__}: {e}，沿用缓存/兜底 official")
        return _server_type_cache or SERVER_OFFICIAL

    product = (data or {}).get('ProductName') if isinstance(data, dict) else None
    result = SERVER_GO_EMBY if _is_go_emby_product(product) else SERVER_OFFICIAL
    _server_type_cache = result
    LOGGER.info(f"go-emby 探测完成: ProductName={product!r} → {result}")
    return result


def get_go_emby_admin() -> Optional[GoEmbyAdmin]:
    """懒加载 `GoEmbyAdmin` 单例（从 config 读地址与管理员凭据）。

    未配管理员凭据时返回 None，并且**只警告一次**（避免每轮调用刷屏）。
    """
    global _admin_singleton, _admin_warned
    if _admin_singleton is not None:
        return _admin_singleton

    url = str(getattr(config, 'emby_url', '') or '').strip()
    user = str(getattr(config, 'emby_admin_user', '') or '').strip()
    password = getattr(config, 'emby_admin_password', '') or ''
    if not url or not user or not password:
        if not _admin_warned:
            _admin_warned = True
            LOGGER.warning(
                "go-emby 管理员凭据未配置（emby_url/emby_admin_user/emby_admin_password），"
                "/admin 接口不可用"
            )
        return None

    _admin_singleton = GoEmbyAdmin(
        base_url=url,
        api_key=getattr(config, 'emby_api', '') or '',
        admin_user=user,
        admin_password=password,
    )
    return _admin_singleton


async def is_go_emby() -> bool:
    """当前服务器是 go-emby **且**管理员凭据已配置时返回 True。"""
    return (await detect_server_type()) == SERVER_GO_EMBY and get_go_emby_admin() is not None


def reset_cache() -> None:
    """清空 token 与探测缓存（供测试用）。"""
    global _server_type_cache, _admin_singleton, _admin_warned
    if _admin_singleton is not None:
        _admin_singleton._token = None
    _server_type_cache = None
    _admin_singleton = None
    _admin_warned = False
