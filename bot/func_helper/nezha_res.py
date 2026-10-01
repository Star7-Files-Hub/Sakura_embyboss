"""
根据哪吒探针项目修改，只是图服务器界面好看。
支持 Nezha V0、V1 API 和 Komari API
"""
import humanize as humanize
import requests as r
import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

# 同步探针请求的超时（秒）：避免探针不可达时永久阻塞
TIMEOUT = 15


class KomariAPI:
    """Komari 探针 API 客户端"""

    def __init__(self, dashboard_url, api_key=None):
        """
        初始化 Komari API 客户端
        :param dashboard_url: Komari 面板地址
        :param api_key: API Key (可选，用于 Bearer 认证访问管理接口)
        """
        self.base_url = dashboard_url.rstrip('/')
        self.api_key = api_key
        self.session = None

    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(self, method, endpoint, **kwargs):
        """发送 API 请求"""
        await self._ensure_session()
        url = f'{self.base_url}/api{endpoint}'
        headers = kwargs.pop('headers', {})
        
        # 如果有 API Key，添加 Bearer 认证
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        try:
            async with self.session.request(method, url, headers=headers, **kwargs) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.warning(f"Komari API 请求失败: {resp.status} - {endpoint}")
                    return None
        except Exception as e:
            logger.error(f"Komari API 请求异常: {e}")
            return None

    async def get_nodes(self):
        """获取所有节点信息列表"""
        data = await self.request('GET', '/nodes')
        return data

    async def get_node_recent(self, uuid):
        """获取指定节点最近1分钟的状态数据"""
        data = await self.request('GET', f'/recent/{uuid}')
        return data

    async def get_version(self):
        """获取 Komari 服务端版本信息"""
        data = await self.request('GET', '/version')
        return data


async def sever_info_komari_async(tz, tz_api, tz_id):
    """
    Komari API: 获取服务器信息
    :param tz: Komari 面板地址
    :param tz_api: API Key (可选)
    :param tz_id: 要显示的节点 UUID 列表 (如果为空则显示所有)
    """
    if not tz:
        return None

    api = KomariAPI(tz, tz_api if tz_api else None)
    b = []
    try:
        # 获取所有节点列表
        nodes_resp = await api.get_nodes()
        if not nodes_resp or nodes_resp.get('status') != 'success':
            logger.warning(f"Komari 获取节点列表失败: {nodes_resp}")
            await api.close()
            return None

        nodes = nodes_resp.get('data', [])
        
        for node in nodes:
            node_uuid = node.get('uuid')
            node_name = node.get('name', '未知节点')
            
            # 如果指定了 tz_id，只显示指定的节点
            if tz_id:
                # tz_id 可以是 UUID 字符串或者数字索引
                if node_uuid not in tz_id and str(nodes.index(node) + 1) not in [str(x) for x in tz_id]:
                    continue

            # 获取节点最近状态
            recent_resp = await api.get_node_recent(node_uuid)
            
            if recent_resp and recent_resp.get('status') == 'success' and recent_resp.get('data'):
                # 获取最新的一条数据
                latest_data = recent_resp['data'][-1] if recent_resp['data'] else None
                
                if latest_data:
                    # 解析数据
                    uptime_sec = latest_data.get('uptime', 0)
                    uptime = f'{int(uptime_sec / 86400)} 天' if uptime_sec > 0 else '⚠️掉线辣'
                    
                    cpu_data = latest_data.get('cpu', {})
                    CPU = f"{cpu_data.get('usage', 0):.2f}"
                    
                    ram_data = latest_data.get('ram', {})
                    mem_total = ram_data.get('total', 0)
                    mem_used = ram_data.get('used', 0)
                    MemTotal = humanize.naturalsize(mem_total, gnu=True)
                    MemUsed = humanize.naturalsize(mem_used, gnu=True)
                    Mempercent = f"{(mem_used / mem_total) * 100:.2f}" if mem_total != 0 else "0"
                    
                    network_data = latest_data.get('network', {})
                    NetInSpeed = humanize.naturalsize(network_data.get('down', 0), gnu=True)
                    NetOutSpeed = humanize.naturalsize(network_data.get('up', 0), gnu=True)
                    NetInTransfer = humanize.naturalsize(network_data.get('totalDown', 0), gnu=True)
                    NetOutTransfer = humanize.naturalsize(network_data.get('totalUp', 0), gnu=True)
                else:
                    uptime = '⚠️掉线辣'
                    CPU = "0.00"
                    MemTotal = "0"
                    MemUsed = "0"
                    Mempercent = "0"
                    NetInTransfer = "0"
                    NetOutTransfer = "0"
                    NetInSpeed = "0"
                    NetOutSpeed = "0"
            else:
                # 没有状态数据，可能离线
                uptime = '⚠️掉线辣'
                CPU = "0.00"
                MemTotal = humanize.naturalsize(node.get('mem_total', 0), gnu=True)
                MemUsed = "0"
                Mempercent = "0"
                NetInTransfer = "0"
                NetOutTransfer = "0"
                NetInSpeed = "0"
                NetOutSpeed = "0"

            # 使用节点的 region 信息
            region = node.get('region', '')
            display_name = f"{region} {node_name}".strip() if region else node_name

            status_msg = f"· 🌐 服务器 | {display_name} · {uptime}\n" \
                         f"· 💫 CPU | {CPU}% \n" \
                         f"· 🌩️ 内存 | {Mempercent}% [{MemUsed}/{MemTotal}]\n" \
                         f"· ⚡ 网速 | ↓{NetInSpeed}/s  ↑{NetOutSpeed}/s\n" \
                         f"· 🌊 流量 | ↓{NetInTransfer}  ↑{NetOutTransfer}\n"
            b.append(dict(name=node_name, id=node_uuid, server=status_msg))

        await api.close()
        return b if b else None
    except Exception as e:
        logger.error(f"Komari 获取服务器信息异常: {e}")
        await api.close()
        return None


class NezhaV1API:
    """Nezha V1 API 客户端"""
    MAX_RETRY = 2  # 最大重试次数，防止无限循环

    def __init__(self, dashboard_url, username, password):
        self.base_url = dashboard_url.rstrip('/') + '/api/v1'
        self.username = username
        self.password = password
        self.token = None
        self.session = None
        self.lock = asyncio.Lock()

    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def authenticate(self):
        async with self.lock:
            if self.token is not None:
                return True
            await self._ensure_session()
            login_url = f'{self.base_url}/login'
            payload = {
                'username': self.username,
                'password': self.password
            }
            try:
                async with self.session.post(login_url, json=payload) as resp:
                    data = await resp.json()
                    if data.get('success'):
                        self.token = data['data']['token']
                        return True
                    else:
                        logger.warning(f"Nezha V1 认证失败: {data.get('message', '未知错误')}")
                        return False
            except Exception as e:
                logger.error(f"Nezha V1 认证异常: {e}")
                return False

    async def request(self, method, endpoint, retry_count=0, **kwargs):
        if not await self.authenticate():
            return None
        await self._ensure_session()
        url = f'{self.base_url}{endpoint}'
        headers = kwargs.pop('headers', {})
        headers['Authorization'] = f'Bearer {self.token}'

        try:
            async with self.session.request(method, url, headers=headers, **kwargs) as resp:
                if resp.status == 401:
                    if retry_count >= self.MAX_RETRY:
                        logger.error(f"Nezha V1 请求重试次数过多: {endpoint}")
                        return None
                    self.token = None
                    return await self.request(method, endpoint, retry_count=retry_count + 1, **kwargs)
                elif resp.status == 200:
                    return await resp.json()
                else:
                    logger.warning(f"Nezha V1 API 请求失败: {resp.status} - {endpoint}")
                    return None
        except Exception as e:
            logger.error(f"Nezha V1 API 请求异常: {e}")
            return None

    async def get_servers(self):
        data = await self.request('GET', '/server')
        return data

    async def get_server_detail(self, server_id):
        servers = await self.get_servers()
        if servers and servers.get('success'):
            for server in servers['data']:
                if server['id'] == server_id:
                    return server
        return None


def sever_info_v0(tz, tz_api, tz_id):
    """V0 API: 使用 token 认证"""
    if not tz or not tz_api or not tz_id: 
        return None
    # 请求头
    tz_headers = {
        'Authorization': tz_api  # 后台右上角下拉菜单获取 API Token
    }
    b = []
    try:
        # 请求地址
        for x in tz_id:
            tz_url = f'{tz}/api/v1/server/details?id={x}'
            # 发送GET请求，获取服务器流量信息
            # 必须设置 timeout：探针不可达时无超时的同步请求会永久挂住事件循环
            res = r.get(tz_url, headers=tz_headers, timeout=TIMEOUT).json()
            detail = res["result"][0]
            """cpu"""
            uptime = f'{int(detail["status"]["Uptime"] / 86400)} 天' if detail["status"]["Uptime"] != 0 else '⚠️掉线辣'
            CPU = f"{detail['status']['CPU']:.2f}"
            """内存"""
            MemTotal = humanize.naturalsize(detail['host']['MemTotal'], gnu=True)
            MemUsed = humanize.naturalsize(detail['status']['MemUsed'], gnu=True)
            Mempercent = f"{(detail['status']['MemUsed'] / detail['host']['MemTotal']) * 100:.2f}" if detail['host'][
                                                                                                          'MemTotal'] != 0 else "0"
            """流量"""
            NetInTransfer = humanize.naturalsize(detail['status']['NetInTransfer'], gnu=True)
            NetOutTransfer = humanize.naturalsize(detail['status']['NetOutTransfer'], gnu=True)
            """网速"""
            NetInSpeed = humanize.naturalsize(detail['status']['NetInSpeed'], gnu=True)
            NetOutSpeed = humanize.naturalsize(detail['status']['NetOutSpeed'], gnu=True)

            status_msg = f"· 🌐 服务器 | {detail['name']} · {uptime}\n" \
                         f"· 💫 CPU | {CPU}% \n" \
                         f"· 🌩️ 内存 | {Mempercent}% [{MemUsed}/{MemTotal}]\n" \
                         f"· ⚡ 网速 | ↓{NetInSpeed}/s  ↑{NetOutSpeed}/s\n" \
                         f"· 🌊 流量 | ↓{NetInTransfer}  ↑{NetOutTransfer}\n"
            b.append(dict(name=f'{detail["name"]}', id=detail["id"], server=status_msg))
        return b
    except Exception as e:
        # 原实现是裸 except，会把所有错误（含解析失败、超时、鉴权失败）静默吞掉
        logger.error(f"Nezha V0 获取服务器信息异常: {e}")
        return None


async def sever_info_v1_async(tz, tz_username, tz_password, tz_id):
    """V1 API: 使用用户名密码认证"""
    if not tz or not tz_username or not tz_password:
        return None

    api = NezhaV1API(tz, tz_username, tz_password)
    b = []
    try:
        servers = await api.get_servers()
        if not servers or not servers.get('success'):
            logger.warning(f"Nezha V1 获取服务器列表失败: {servers}")
            await api.close()
            return None

        for server in servers['data']:
            # 如果指定了 tz_id，只显示指定的服务器
            if tz_id and server['id'] not in [int(x) for x in tz_id]:
                continue

            # V1 API 数据结构
            state = server.get('state', {})
            host = server.get('host', {})
            
            # 判断在线状态
            # V1 中使用 state 字段判断在线状态
            if state:
                uptime = f'{int(state.get("uptime", 0) / 86400)} 天' if state.get("uptime", 0) != 0 else '⚠️掉线辣'
                CPU = f"{state.get('cpu', 0):.2f}"
                
                mem_total = host.get('mem_total', 0)
                mem_used = state.get('mem_used', 0)
                MemTotal = humanize.naturalsize(mem_total, gnu=True)
                MemUsed = humanize.naturalsize(mem_used, gnu=True)
                Mempercent = f"{(mem_used / mem_total) * 100:.2f}" if mem_total != 0 else "0"
                
                NetInTransfer = humanize.naturalsize(state.get('net_in_transfer', 0), gnu=True)
                NetOutTransfer = humanize.naturalsize(state.get('net_out_transfer', 0), gnu=True)
                
                NetInSpeed = humanize.naturalsize(state.get('net_in_speed', 0), gnu=True)
                NetOutSpeed = humanize.naturalsize(state.get('net_out_speed', 0), gnu=True)
            else:
                uptime = '⚠️掉线辣'
                CPU = "0.00"
                MemTotal = "0"
                MemUsed = "0"
                Mempercent = "0"
                NetInTransfer = "0"
                NetOutTransfer = "0"
                NetInSpeed = "0"
                NetOutSpeed = "0"

            status_msg = f"· 🌐 服务器 | {server['name']} · {uptime}\n" \
                         f"· 💫 CPU | {CPU}% \n" \
                         f"· 🌩️ 内存 | {Mempercent}% [{MemUsed}/{MemTotal}]\n" \
                         f"· ⚡ 网速 | ↓{NetInSpeed}/s  ↑{NetOutSpeed}/s\n" \
                         f"· 🌊 流量 | ↓{NetInTransfer}  ↑{NetOutTransfer}\n"
            b.append(dict(name=f'{server["name"]}', id=server["id"], server=status_msg))
        
        await api.close()
        return b if b else None
    except Exception as e:
        logger.error(f"Nezha V1 获取服务器信息异常: {e}")
        await api.close()
        return None


async def sever_info(tz, tz_api, tz_id, tz_version="v0", tz_username=None, tz_password=None):
    """
    获取服务器信息的统一入口
    :param tz: 探针地址
    :param tz_api: V0 API Token / Komari API Key
    :param tz_id: 服务器ID列表
    :param tz_version: API版本，"v0"、"v1" 或 "komari"
    :param tz_username: V1 用户名
    :param tz_password: V1 密码
    :return: 服务器信息列表
    """
    logger.debug(f"使用探针 API 版本: {tz_version}")
    if tz_version == "v1":
        # V1 使用异步调用
        return await sever_info_v1_async(tz, tz_username, tz_password, tz_id)
    elif tz_version == "komari":
        # Komari 使用异步调用
        return await sever_info_komari_async(tz, tz_api, tz_id)
    else:
        # 默认使用 V0 API。
        # V0 用的是同步 requests，必须丢到线程里执行，否则会阻塞整个事件循环。
        return await asyncio.to_thread(sever_info_v0, tz, tz_api, tz_id)
