"""
Tracearr 对接模块

提供与 Tracearr 的集成功能：
- 通过 Tracearr API 获取会话信息
- 通过 Tracearr API 终止会话（作为 Emby API 的备选方案）
- 同步用户播放数据

注意：Tracearr 的终止会话 API 会检查客户端是否支持远程控制（SupportsRemoteControl），
如果客户端不支持，Tracearr 会返回错误 "Client does not support remote control"。
这是 Tracearr 的安全设计，避免虚假报告终止成功。

EmbyBoss 的终止方式不检查此字段，直接发送停止命令，因此可以"强制"终止。
两种方式各有优劣，可根据需要选择。

Author: embyboss
"""

import aiohttp
from bot import config, LOGGER


class TracearrClient:
    """Tracearr API 客户端"""

    def __init__(self, base_url: str = None, api_key: str = None):
        self.base_url = (base_url or config.tracearr_url or "").rstrip('/')
        self.api_key = api_key or config.tracearr_api_key or ""
        self._session: aiohttp.ClientSession = None

    @property
    def enabled(self):
        return bool(config.tracearr_enabled and self.base_url and self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, endpoint: str, **kwargs):
        """统一请求方法"""
        if not self.enabled:
            return False, "Tracearr 未启用或配置不完整"

        url = f"{self.base_url}/api{endpoint}"
        session = await self._get_session()

        try:
            async with session.request(method, url, **kwargs) as response:
                if response.status in (200, 201, 204):
                    if response.content_type == 'application/json':
                        data = await response.json()
                        return True, data
                    return True, None
                else:
                    error_text = await response.text()
                    return False, f"HTTP {response.status}: {error_text}"
        except aiohttp.ClientError as e:
            return False, f"网络错误: {str(e)}"
        except Exception as e:
            return False, f"未知错误: {str(e)}"

    async def get_sessions(self):
        """
        获取所有活跃会话
        :return: (success, sessions_list or error_msg)
        """
        return await self._request("GET", "/sessions")

    async def get_user_sessions(self, server_user_id: str = None):
        """
        获取指定用户的活跃会话
        :param server_user_id: Tracearr 中的 server user ID
        :return: (success, sessions_list or error_msg)
        """
        params = {}
        if server_user_id:
            params["serverUserId"] = server_user_id
        return await self._request("GET", "/sessions", params=params)

    async def terminate_session(self, session_id: str, reason: str = "Concurrent play limit exceeded"):
        """
        终止指定会话（通过 Tracearr）
        
        注意：Tracearr 会先检查客户端是否支持远程控制。
        如果客户端不支持，会返回 "Client does not support remote control" 错误。
        
        :param session_id: Tracearr 中的 session UUID
        :param reason: 终止原因
        :return: (success, result or error_msg)
        """
        return await self._request(
            "POST",
            f"/sessions/{session_id}/terminate",
            json={"reason": reason}
        )

    async def get_servers(self):
        """
        获取所有已配置的媒体服务器
        :return: (success, servers_list or error_msg)
        """
        return await self._request("GET", "/servers")

    async def get_users(self):
        """
        获取所有用户
        :return: (success, users_list or error_msg)
        """
        return await self._request("GET", "/users")

    async def get_violations(self, acknowledged: bool = None):
        """
        获取违规记录
        :param acknowledged: None=全部, True=已确认, False=未确认
        :return: (success, violations_list or error_msg)
        """
        params = {}
        if acknowledged is not None:
            params["acknowledged"] = str(acknowledged).lower()
        return await self._request("GET", "/violations", params=params)


# 全局实例
tracearr = TracearrClient()


async def tracearr_terminate_fallback(emby_session_id: str, tracearr_session_id: str = None, reason: str = "Concurrent play limit exceeded"):
    """
    通过 Tracearr 终止会话（作为 Emby API 的备选方案）
    
    如果 Emby API 终止失败，可以尝试通过 Tracearr 终止。
    但需要注意：Tracearr 会检查客户端是否支持远程控制。
    
    :param emby_session_id: Emby 会话ID
    :param tracearr_session_id: Tracearr 会话UUID（可选，如果不提供则尝试通过 Emby ID 查找）
    :param reason: 终止原因
    :return: (success, message)
    """
    if not tracearr.enabled:
        return False, "Tracearr 未启用"

    if tracearr_session_id:
        ok, result = await tracearr.terminate_session(tracearr_session_id, reason)
        if ok:
            return True, "通过 Tracearr 成功终止会话"
        return False, f"Tracearr 终止失败: {result}"

    # 如果没有提供 tracearr_session_id，尝试通过 sessions 查找
    ok, sessions = await tracearr.get_sessions()
    if not ok:
        return False, f"获取 Tracearr 会话列表失败: {sessions}"

    target = None
    for s in sessions:
        # 尝试匹配 Emby 会话ID
        if s.get("sessionKey") == emby_session_id or s.get("id") == emby_session_id:
            target = s
            break

    if not target:
        return False, f"在 Tracearr 中未找到对应的会话: {emby_session_id}"

    session_uuid = target.get("id")
    ok, result = await tracearr.terminate_session(session_uuid, reason)
    if ok:
        return True, "通过 Tracearr 成功终止会话"
    return False, f"Tracearr 终止失败: {result}"
