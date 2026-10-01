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
from urllib.parse import urlparse

from bot import config, LOGGER

# 只允许 http/https：tracearr_url 由管理员在面板任意填写，
# 不做限制时配置错误会把 Bearer API Key 发往任意地址（如 file:// 或内网元数据地址）。
_ALLOWED_SCHEMES = ("http", "https")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _unwrap_list(data):
    """把公开 API 的列表响应统一拆成裸列表。

    实机核对（Tracearr v2.5.1）：/users、/violations、/streams 的响应是
    {"data": [...], "meta"/"summary": {...}} 这样的信封结构。
    旧代码直接把整个 dict 当列表用，`for s in sessions:` 会去迭代 dict 的键
    （字符串），后续 s.get(...) 必然 AttributeError —— 属于静默失效。
    """
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return inner
        # /streams?summary=... 只返回 summary，没有 data
        return []
    if isinstance(data, list):
        return data
    return []



class TracearrClient:
    """Tracearr API 客户端"""

    def __init__(self, base_url: str = None, api_key: str = None):
        raw_base_url = (base_url or config.tracearr_url or "").rstrip('/')
        self.api_key = api_key or config.tracearr_api_key or ""
        self._session: aiohttp.ClientSession = None
        self.base_url = raw_base_url

        # 校验地址合法性：scheme 必须是 http/https 且要有主机名
        if raw_base_url:
            parsed = urlparse(raw_base_url)
            if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
                LOGGER.error(
                    f"Tracearr 地址不合法，已禁用对接（必须是 http/https 且包含主机名）: {raw_base_url}"
                )
                self.base_url = ""
            elif parsed.scheme != "https" and parsed.hostname not in _LOOPBACK_HOSTS:
                LOGGER.warning(
                    "Tracearr 使用明文 HTTP，Bearer API Key 将以明文传输，建议改用 HTTPS。"
                )

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

        # API 前缀必须是 /api/v1/public（"Tracearr Public API"）。
        # 实机核对（Tracearr v2.5.1，2026-10，取自 /api/v1/public/docs 的 OpenAPI 3.0 规范）：
        #   /api/sessions              -> 404  （原来的写法，对接完全失效且被静默吞掉）
        #   /api/v1/sessions           -> 401  （管理端 API，需要会话 JWT，不接受公开 Key）
        #   /api/v1/public/streams     -> 200  ← 公开 API 的真实路径
        # 公开 API 只有 9 个端点，且**没有 sessions / servers**，
        # 活跃会话叫 streams（见下方各方法）。
        url = f"{self.base_url}/api/v1/public{endpoint}"
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
                    # 不把上游响应正文回传给调用方（可能含敏感信息），只记录到日志
                    LOGGER.error(f"Tracearr 请求失败 HTTP {response.status}: {error_text[:500]}")
                    return False, f"Tracearr 请求失败（HTTP {response.status}）"
        except aiohttp.ClientError as e:
            return False, f"网络错误: {str(e)}"
        except Exception as e:
            return False, f"未知错误: {str(e)}"

    async def get_health(self):
        """
        获取服务健康状态与已配置服务器列表（GET /health）
        :return: (success, {status, version, timestamp, servers:[...]} or error_msg)
        """
        return await self._request("GET", "/health")

    async def get_sessions(self):
        """
        获取所有活跃会话。

        注意：Tracearr 公开 API 里没有 sessions，活跃会话对应 **streams**。
        响应形如 {"data": [ ... ], "summary": {...}}，这里统一返回 data 列表。
        :return: (success, streams_list or error_msg)
        """
        ok, data = await self._request("GET", "/streams")
        if not ok:
            return ok, data
        return True, _unwrap_list(data)

    async def get_user_sessions(self, server_id: str = None):
        """
        获取活跃会话（可按媒体服务器过滤）。

        注意语义变更：公共 API 的 /streams 只支持按 **serverId**（媒体服务器 UUID）
        过滤，**不支持**按 Tracearr 用户 ID 过滤（旧代码传的 serverUserId 并不存在，
        会被 Fastify 忽略从而退化成"返回全部会话"）。如需按用户筛选，
        请在调用方用返回结果的 username / userId 自行过滤。

        :param server_id: 媒体服务器 ID（servers[].id，取自 get_health()）
        :return: (success, streams_list or error_msg)
        """
        params = {}
        if server_id:
            params["serverId"] = server_id
        ok, data = await self._request("GET", "/streams", params=params)
        if not ok:
            return ok, data
        return True, _unwrap_list(data)

    async def terminate_session(self, session_id: str, reason: str = "Concurrent play limit exceeded"):
        """
        终止指定会话（通过 Tracearr）

        实机核对：端点是 POST /streams/{id}/terminate，请求体 {"reason": str}，
        成功返回 {"success": bool, "terminationLogId": str, "message": str}。

        注意：Tracearr 会先检查客户端是否支持远程控制（SupportsRemoteControl）。
        如果客户端不支持，会返回错误而不会虚报成功。

        :param session_id: Tracearr 中的 stream UUID（即会话 id）
        :param reason: 终止原因
        :return: (success, result or error_msg)
        """
        return await self._request(
            "POST",
            f"/streams/{session_id}/terminate",
            json={"reason": reason}
        )

    async def get_servers(self):
        """
        获取所有已配置的媒体服务器。

        注意：公开 API **没有 /servers 端点**（实测 404），服务器列表在 /health 的
        servers 字段里，这里自动取出该列表。
        :return: (success, servers_list or error_msg)
        """
        ok, data = await self._request("GET", "/health")
        if not ok:
            return ok, data
        if isinstance(data, dict):
            return True, data.get("servers") or []
        return True, []

    async def get_users(self, page: int = None, page_size: int = None):
        """
        获取所有用户（GET /users）
        :return: (success, users_list or error_msg)
        """
        params = {}
        if page:
            params["page"] = page
        if page_size:
            params["pageSize"] = page_size
        ok, data = await self._request("GET", "/users", params=params or None)
        if not ok:
            return ok, data
        return True, _unwrap_list(data)

    async def get_violations(self, acknowledged: bool = None):
        """
        获取违规记录（GET /violations）
        :param acknowledged: None=全部, True=已确认, False=未确认
        :return: (success, violations_list or error_msg)
        """
        params = {}
        if acknowledged is not None:
            params["acknowledged"] = str(acknowledged).lower()
        ok, data = await self._request("GET", "/violations", params=params or None)
        if not ok:
            return ok, data
        return True, _unwrap_list(data)

    async def get_stats(self):
        """获取 Dashboard 统计（GET /stats）"""
        return await self._request("GET", "/stats")

    async def get_history(self, page: int = None, page_size: int = None):
        """获取会话历史（GET /history）"""
        params = {}
        if page:
            params["page"] = page
        if page_size:
            params["pageSize"] = page_size
        ok, data = await self._request("GET", "/history", params=params or None)
        if not ok:
            return ok, data
        return True, _unwrap_list(data)


# 全局实例
tracearr = TracearrClient()


async def tracearr_terminate_fallback(emby_session_id: str, tracearr_session_id: str = None, reason: str = "Concurrent play limit exceeded"):
    """
    通过 Tracearr 终止会话（作为 Emby API 的备选方案）

    如果 Emby API 终止失败，可以尝试通过 Tracearr 终止。
    但需要注意：Tracearr 会检查客户端是否支持远程控制。

    注意：Tracearr 的 stream id 是它自己的 UUID，**不等于** Emby 的会话 ID，
    因此除了直接传 tracearr_session_id 外，按 Emby 会话 ID 反查通常匹配不到
    （Tracearr 公开 API 的 Stream 对象里没有 sessionKey 字段）。

    :param emby_session_id: Emby 会话ID
    :param tracearr_session_id: Tracearr 的 stream UUID（推荐直接提供）
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

    # 没有直接给出 tracearr_session_id 时，退化为按 id 反查（多数情况下匹配不到）
    ok, streams = await tracearr.get_sessions()
    if not ok:
        return False, f"获取 Tracearr 会话列表失败: {streams}"
    if not isinstance(streams, list):
        # 兜底：响应不是预期结构时不要迭代出意外结果（旧代码会迭代 dict 的键）
        return False, f"Tracearr 会话列表结构异常: {type(streams).__name__}"

    target = None
    for s in streams:
        if not isinstance(s, dict):
            continue
        if s.get("id") == emby_session_id:
            target = s
            break

    if not target:
        return False, f"在 Tracearr 中未找到对应的会话: {emby_session_id}"

    session_uuid = target.get("id")
    ok, result = await tracearr.terminate_session(session_uuid, reason)
    if ok:
        return True, "通过 Tracearr 成功终止会话"
    return False, f"Tracearr 终止失败: {result}"
