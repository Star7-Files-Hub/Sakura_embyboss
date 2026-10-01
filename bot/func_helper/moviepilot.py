import requests
import json
from urllib.parse import quote, urlencode
from bot import LOGGER, moviepilot, save_config
import aiohttp
import asyncio

# 添加配置类
class MoviePilot:
    def __init__(self):
        self.url = moviepilot.url
        self.username = moviepilot.username 
        self.password = moviepilot.password
        self.access_token = moviepilot.access_token or ''

mp = MoviePilot()

TIMEOUT = 30
# aiohttp重试装饰器
def aiohttp_retry(retry_count):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for i in range(retry_count):
                try:
                    return await func(*args, **kwargs)
                except aiohttp.ClientError:
                    await asyncio.sleep(3)  # 延迟 3 秒后进行重试
            return None

        return wrapper

    return decorator
@aiohttp_retry(3)
async def _do_request(request):
    async with aiohttp.ClientSession() as session:
        async with session.request(method=request['method'], url=request['url'], headers=request['headers'], data=request.get('data')) as response:
            if response.status == 401 or response.status == 403:
                LOGGER.error("MP Token过期, 尝试重新登录.")
                success = await login()
                if success:
                    request['headers']['Authorization'] = mp.access_token
                    return await _do_request(request)
                return None
            return await response.json()
def _login_sync(url, payload, headers):
    """同步的登录请求，放到线程里执行，避免阻塞事件循环。"""
    return requests.post(url, data=payload, headers=headers, timeout=TIMEOUT)


async def login():
    url = f"{mp.url}/api/v1/login/access-token"
    # 用户名/密码必须做表单编码，否则包含 & = # 等字符时会构造出错误的请求体
    payload = urlencode({"username": mp.username or "", "password": mp.password or ""})
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    try:
        # 原实现直接调用同步 requests，会阻塞整个 asyncio 事件循环（含 bot 消息处理）
        response = await asyncio.to_thread(_login_sync, url, payload, headers)
        result = response.json()
    except Exception as e:
        LOGGER.error(f"MP 登录请求失败: {e}")
        return False
    if 'access_token' in result:
        mp.access_token = result['token_type'] + ' ' + result['access_token']
        moviepilot.access_token = mp.access_token # 保存到config
        save_config()
        LOGGER.info("MP 登录成功, token已保存")
        return True
    else:
        LOGGER.error(f"MP 登录失败: {result}")
        return False

async def search(title):
    """
    搜索资源
    Args:
        title: 搜索关键词
    Returns:
        (success, results)
        success: bool 是否成功
        results: list 搜索结果列表
    """
    if title is None:
        return False, []
        
    # 关键词必须 URL 编码：否则关键词里的 & # 空格 等字符会改变查询语义（可注入额外参数）
    url = f"{mp.url}/api/v1/search/title?keyword={quote(str(title), safe='')}"
    headers = {'Authorization': mp.access_token}
    request = {'method': 'GET', 'url': url, 'headers': headers}
    try:
        data = await _do_request(request)
        results = []
        if data.get("success", False):
            data = data["data"]
            for item in data:
                meta_info = item.get("meta_info", {})
                torrent_info = item.get("torrent_info", {})
                
                seeders = torrent_info.get("seeders", "0")
                try:
                    seeders = int(seeders) if seeders else 0
                except (ValueError, TypeError):
                    seeders = 0
                result = {
                    "title": meta_info.get("title", ""),
                    "year": meta_info.get("year", ""),
                    "type": meta_info.get("type", ""),
                    "resource_pix": meta_info.get("resource_pix", ""),
                    "video_encode": meta_info.get("video_encode", ""),
                    "audio_encode": meta_info.get("audio_encode", ""),
                    "resource_team": meta_info.get("resource_team", ""),
                    "seeders": seeders,
                    "size": torrent_info.get("size", "0"),
                    "labels": torrent_info.get("labels", ""),
                    "description": torrent_info.get("description", ""),
                    "torrent_info": torrent_info,
                }
                results.append(result)
                
        # 只按做种数排序,移除数量限制
        results.sort(key=lambda x: x["seeders"], reverse=True)
            
        LOGGER.info("MP Search successful!")
        return True, results
    except Exception as e:
        LOGGER.error(f"MP Search failed: {str(e)}")
        return False, []


async def add_download_task(param):
    if param is None:
        return False, None
    url = f"{mp.url}/api/v1/download/add"
    headers = {'Content-Type': 'application/json',
               'Authorization': mp.access_token}
    jsonData = json.dumps(param)
    request = {'method': 'POST', 'url': url,
               'headers': headers, 'data': jsonData}
    try:
        result = await _do_request(request)
        if result.get("success", False):
            LOGGER.info(f"MP 添加下载任务成功, ID: {result['data']['download_id']}")
            return True, result["data"]["download_id"]
        else:
            LOGGER.error(f"MP 添加下载任务失败: {result}")
            return False, None
    except Exception as e:
        LOGGER.error(f"MP 添加下载任务失败: {e}")
        return False, None

async def get_download_task():
    url = f"{mp.url}/api/v1/download?name=下载"
    headers = {'Authorization': mp.access_token}
    request = {'method': 'GET', 'url': url, 'headers': headers}
    try:
        result = await _do_request(request)
        data = []
        for item in result:
            data.append(
                {'download_id': item['hash'],
                 'state': item['state'],
                 'progress': item['progress'],
                 'left_time': item['left_time']
                 })
        return data
    except Exception as e:
        LOGGER.error(f"MP 获取下载任务失败: {e}")
        return None
async def get_history_transfer_task_by_title_download_id(title, download_id, page = 1, count = 50):
    url = f"{mp.url}/api/v1/history/transfer?title={quote(str(title), safe='')}&page={page}&count={count}"
    headers = {'Authorization': mp.access_token}
    request = {'method': 'GET', 'url': url, 'headers': headers}
    try:
        result = await _do_request(request)
        if result and result.get("success", False) and result.get("data", []):
            for item in result["data"]["list"]:
                if item['download_hash'] == download_id:
                    return item['status']
            return None
        else:
            LOGGER.error(f"MP 获取历史转移任务失败: {result}")
            return None
    except Exception as e:
        LOGGER.error(f"MP 获取历史转移任务失败: {e}")
        return None