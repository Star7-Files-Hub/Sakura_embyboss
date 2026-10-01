#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
get_user_ban - 
Author:susu
Date:2024/8/27
"""
import re
from typing import Optional

import aiohttp
from fastapi import APIRouter, Request
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby
from bot import LOGGER, group, bot
from bot.func_helper.emby import emby
from datetime import datetime

route = APIRouter()

# 该端点只应由反向代理调用（鉴权见 bot/web/api/__init__.py 的 verify_internal）。
# eid 由上游请求参数透传而来，这里仍做格式约束：
# 避免把任意字符串透传给 Emby API 路径或拼进 Telegram 消息（注入/伪造日志）。
_EMBY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

# X-Emby-Authorization 形如：MediaBrowser Client="...", Device="...", Token="xxxx"
_EMBY_AUTH_TOKEN_PATTERN = re.compile(r'Token="([^"]+)"', re.IGNORECASE)


def _extract_emby_token(request: Request) -> Optional[str]:
    """取出调用方自己的 Emby 令牌（由反向代理转发）。

    优先 `X-Emby-Token`；回退解析 `X-Emby-Authorization` 里的 `Token="..."`。
    """
    token = request.headers.get("X-Emby-Token")
    if token and token.strip():
        return token.strip()
    auth = request.headers.get("X-Emby-Authorization") or ""
    matched = _EMBY_AUTH_TOKEN_PATTERN.search(auth)
    if matched:
        return matched.group(1).strip() or None
    return None


async def _resolve_own_emby_id(token: str) -> Optional[str]:
    """用调用方自己的令牌向 Emby 询问「你是谁」，返回其 Emby 用户 Id。

    这是 eid 归属校验唯一可信的来源：令牌是密钥，客户端无法伪造他人的令牌。
    """
    url = f"{emby.url}/Users/Me"
    headers = {"X-Emby-Token": token, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    LOGGER.warning(f"ban_playlist 校验归属：Emby /Users/Me 返回 {resp.status}")
                    return None
                data = await resp.json()
        own_id = data.get("Id") if isinstance(data, dict) else None
        return str(own_id) if own_id else None
    except Exception as e:
        LOGGER.error(f"ban_playlist 校验归属：请求 Emby /Users/Me 失败: {e}")
        return None


@route.get("/ban_playlist")
async def ban_playlist(request: Request, eid: str):
    """
    获取传入的embyid，然后执行查询，删除，发送消息至tg群组
    """
    if not eid:
        return {"user_id": None, "embyid": None, "is_baned": False}

    eid = eid.strip()
    if not _EMBY_ID_PATTERN.match(eid):
        LOGGER.warning("ban_playlist 收到不符合格式的 eid 参数，已拒绝处理")
        return {"user_id": None, "embyid": None, "is_baned": False,
                "details": "eid 参数格式不合法"}

    # ---- eid 归属校验（审计 C-1 闭环）----
    # eid 来自客户端可控的 `?userId=`，而本端点的副作用是「封禁任意 Emby 账号」。
    # 内部令牌只能挡住绕过 nginx 直连 bot 端口的人，挡不住直接请求线路域名、
    # 在 query 里塞入受害者 userId 的攻击者——nginx 会带着合法内部令牌替他完成封禁。
    # 因此这里要求：调用方必须出示自己的 Emby 令牌，且该令牌在 Emby 侧的归属
    # 必须与 eid 一致，否则一律不执行封禁。
    caller_token = _extract_emby_token(request)
    if not caller_token:
        LOGGER.warning("ban_playlist 未收到调用方 Emby 令牌，无法证实 eid 归属，已拒绝执行封禁")
        return {"user_id": None, "embyid": eid, "is_baned": False,
                "details": "无法证实 eid 归属（缺少 Emby 令牌），已拒绝执行封禁"}

    own_emby_id = await _resolve_own_emby_id(caller_token)
    if not own_emby_id or own_emby_id.lower() != eid.lower():
        LOGGER.warning("ban_playlist 的 eid 与调用方 Emby 令牌归属不一致，已拒绝执行封禁")
        return {"user_id": None, "embyid": eid, "is_baned": False,
                "details": "eid 与调用方身份不一致，已拒绝执行封禁"}

    # 注意：sql_get_emby 会同时按 tg / name / embyid 匹配，
    # 因此这里的 eid 也可能命中 TG ID 或用户名（详见审计报告 C 组）。
    user = sql_get_emby(eid)
    lv_display = {'a': '白名单', 'b': '普通用户', 'c': '封禁用户', 'd': '未注册'}
    if user is None:
        details = ''
        if await emby.emby_change_policy(emby_id=eid, disable=True):
            details += "已拦截到疑似敏感操作播放列表，未在emby数据库中找到此数据，但已斩杀该用户（封禁）"
        else:
            details += "已拦截到疑似敏感操作播放列表，未在emby数据库中找到此数据，未能斩杀该用户（封禁）。详细时间见log记录，请手动斩杀。"
        info = {"user_id": None, "embyid": None, "is_baned": False, "details": details}
        text = (
            f"🚫 新建播放列表拦截\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 用户: Unknown\n"
            f"🆔 Emby ID: {eid}\n"
            f"📱 TG ID: Unknown\n"
            f"🏷️ 用户等级: 未注册\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚨 处理措施: {details}\n"
            f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await bot.send_message(chat_id=group[0], text=text)
        LOGGER.warning(text)
        return info

    if await emby.emby_change_policy(emby_id=eid, disable=True):
        action = "✅ 已封禁用户"
        info = {"user_id": user.tg, "emby_name": user.name, "embyid": eid, "is_baned": True,
                "details": "已拦截疑似敏感操作播放列表，用户已被斩杀（封禁）。请向权限管理员描述信息。"}
        text = (
            f"🚫 新建播放列表拦截\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 用户: {user.name}\n"
            f"🆔 Emby ID: {eid}\n"
            f"📱 TG ID: [{user.tg}](tg://user?id={user.tg})\n"
            f"🏷️ 用户等级: {lv_display.get(user.lv, '未知')}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚨 处理措施: {action}\n"
            f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            out = await bot.send_message(group[0], text)
            await out.forward(user.tg)
            sql_update_emby(Emby.tg == info["user_id"], lv='c')
        except Exception as e:
            text += str(e)

    else:
        action = "❌ 封禁失败，请手动处理"
        info = {"user_id": user.tg, "emby_name": user.name, "embyid": eid, "is_baned": False,
                "details": "已拦截疑似敏感操作播放列表，斩杀（封禁）失败，请手动处理。"}
        text = (
            f"🚫 新建播放列表拦截\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 用户: {user.name}\n"
            f"🆔 Emby ID: {eid}\n"
            f"📱 TG ID: [{user.tg}](tg://user?id={user.tg})\n"
            f"🏷️ 用户等级: {lv_display.get(user.lv, '未知')}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚨 处理措施: {action}\n"
            f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            out = await bot.send_message(group[0], text)
            await out.forward(user.tg)
        except Exception as e:
            text += str(e)
    LOGGER.warning(text)
    return info
