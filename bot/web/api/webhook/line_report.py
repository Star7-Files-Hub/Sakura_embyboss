#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
line_report - 接收 nginx mirror 转发的线路访问信息，用于检测用户的播放线路
Author: dddddluo
Date:2026/4/15

使用方式：
    nginx 中在每条线路的 server 块中，对播放相关的 location 使用 mirror 指令，
    将请求的 userId 和对应的线路名称转发到此端点。
    Bot 收到后进行线路权限检查，若违规则下发停止指令/封禁用户。
"""
from fastapi import APIRouter, Header
from bot.sql_helper.sql_emby import Emby, sql_get_emby, sql_update_emby
from bot import LOGGER, bot, config
from bot.func_helper.emby import emby
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

router = APIRouter()

# 违规冷却缓存: {user_id: last_violation_time}
_violation_cooldown: Dict[str, datetime] = {}


def is_in_cooldown(user_id: str) -> bool:
    """检查用户是否在冷却期内（冷却期内的重复上报直接忽略）"""
    cooldown_seconds = getattr(config, "line_filter_cooldown_seconds", 60)
    last_time = _violation_cooldown.get(user_id)
    if last_time and datetime.now() - last_time < timedelta(seconds=cooldown_seconds):
        return True
    return False


def update_cooldown(user_id: str) -> None:
    """更新用户的冷却时间戳，并清理过期条目"""
    cooldown_seconds = getattr(config, "line_filter_cooldown_seconds", 60)
    _violation_cooldown[user_id] = datetime.now()
    # 顺手清理已过期的条目，防止内存无限增长
    expired = [
        uid for uid, t in _violation_cooldown.items()
        if datetime.now() - t >= timedelta(seconds=cooldown_seconds)
    ]
    for uid in expired:
        del _violation_cooldown[uid]

# ==================== 线路权限控制 ====================

def extract_host_port(url: str) -> Tuple[Optional[str], Optional[int]]:
    """从URL中提取主机名和端口"""
    try:
        if not url:
            return None, None
        if not url.startswith(('http://', 'https://')):
            url = f"http://{url}"
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
        return host, port
    except Exception as e:
        LOGGER.error(f"解析URL失败: {url} - {str(e)}")
        return None, None


def is_whitelist_line(session_server_address: str) -> bool:
    """
    检查会话使用的是否是白名单线路

    按 (hostname, port) 精确比对，不使用子串匹配：
    子串匹配会让短域名（如配置 vip.example.com 时的 example.com）被误判为白名单，
    也会让 vip.example.com.evil.tld 这类构造出来的 host 命中白名单。

    :param session_server_address: 会话中的服务器地址
    :return: True 如果是白名单线路
    """
    whitelist_line = getattr(config, "emby_whitelist_line", None)
    if not whitelist_line:
        return False

    session_host, session_port = extract_host_port(session_server_address)
    if not session_host:
        return False
    session_host = session_host.lower()

    # 支持字符串或列表两种配置格式
    lines = whitelist_line if isinstance(whitelist_line, list) else [whitelist_line]
    for line in lines:
        whitelist_host, whitelist_port = extract_host_port(line)
        if not whitelist_host:
            continue
        if whitelist_host.lower() != session_host:
            continue
        # 端口：未显式配置端口时，认为该主机名的任意端口都算白名单线路
        if whitelist_port is None or whitelist_port == session_port:
            return True
    return False


def is_user_whitelisted(user_details: Emby) -> bool:
    """
    检查用户是否是白名单用户
    :param user_details: 用户详情
    :return: True 如果是白名单用户 (lv='a')
    """
    if not user_details:
        return False
    return user_details.lv == 'a'


async def get_session_server_address(session_id: str) -> Optional[str]:
    """
    通过 Emby API 获取会话的服务器地址
    :param session_id: 会话ID
    :return: 服务器地址或None
    """
    try:
        result = await emby._request('GET', '/emby/Sessions')
        if not result.success or not result.data:
            LOGGER.error(f"获取会话信息失败: {result.error}")
            return None
        for session in result.data:
            if session.get("Id") == session_id:
                LOGGER.debug(f"Session详情: {json.dumps(session, ensure_ascii=False, indent=2)}")
                return None
        return None
    except Exception as e:
        LOGGER.error(f"获取会话服务器地址异常: {str(e)}")
        return None


def parse_emby_authorization(auth_header: str) -> Dict[str, str]:
    """解析 Emby Authorization/X-Emby-Authorization 头"""
    if not auth_header:
        return {}

    matches = re.findall(r'([A-Za-z][A-Za-z0-9]*)="([^"]*)"', auth_header)
    return {key: value for key, value in matches if value}


def parse_original_request_uri(request_uri: str) -> Dict[str, str]:
    """从 nginx 转发的原始 request_uri 中提取查询参数"""
    if not request_uri:
        return {}

    try:
        parsed = urlparse(request_uri)
        query = parse_qs(parsed.query, keep_blank_values=False)
        return {key: values[0] for key, values in query.items() if values and values[0]}
    except Exception as e:
        LOGGER.error(f"解析原始 request_uri 失败: {request_uri} - {e}")
        return {}


def redact_request_uri(request_uri: str) -> str:
    """对 request_uri 中的敏感查询参数做脱敏，避免日志泄露 token/api_key"""
    if not request_uri:
        return ""

    try:
        parsed = urlparse(request_uri)
        query = parse_qs(parsed.query, keep_blank_values=True)
        sensitive_keys = {"api_key", "X-Emby-Token", "x-emby-token", "token"}

        redacted_query = []
        for key, values in query.items():
            if key in sensitive_keys:
                redacted_query.extend((key, "***") for _ in values)
            else:
                redacted_query.extend((key, value) for value in values)

        redacted_query_string = "&".join(f"{key}={value}" for key, value in redacted_query)
        redacted_uri = parsed.path or ""
        if redacted_query_string:
            redacted_uri = f"{redacted_uri}?{redacted_query_string}"
        if parsed.fragment:
            redacted_uri = f"{redacted_uri}#{parsed.fragment}"
        return redacted_uri
    except Exception as e:
        LOGGER.error(f"脱敏原始 request_uri 失败: {request_uri} - {e}")
        return "<redacted>"


def normalize_identifier(value: Optional[str]) -> str:
    """统一清洗用于匹配的标识符"""
    if value is None:
        return ""
    return str(value).strip()


async def fetch_active_sessions() -> List[Dict[str, Any]]:
    """获取当前活跃会话列表"""
    try:
        result = await emby._request("GET", "/emby/Sessions")
        if result.success and isinstance(result.data, list):
            return result.data
        LOGGER.error(f"获取活跃会话失败: {result.error}")
        return []
    except Exception as e:
        LOGGER.error(f"获取活跃会话异常: {e}")
        return []


def find_matching_session(
    sessions: List[Dict[str, Any]],
    *,
    user_id: str = "",
    device_id: str = "",
    session_id: str = "",
    play_session_id: str = "",
    token: str = "",
) -> Optional[Dict[str, Any]]:
    """根据多个线索在活跃会话中匹配最可能的会话"""
    normalized_user_id = normalize_identifier(user_id)
    normalized_device_id = normalize_identifier(device_id)
    normalized_session_id = normalize_identifier(session_id)
    normalized_play_session_id = normalize_identifier(play_session_id)
    normalized_token = normalize_identifier(token)

    def _match_value(session_value: Any, expected: str) -> bool:
        return bool(expected) and normalize_identifier(session_value) == expected

    def _session_matches(session: Dict[str, Any]) -> bool:
        play_state = session.get("PlayState") or {}
        candidates = (
            _match_value(session.get("UserId"), normalized_user_id),
            _match_value(session.get("DeviceId"), normalized_device_id),
            _match_value(session.get("Id"), normalized_session_id),
            _match_value(session.get("PlaySessionId"), normalized_play_session_id),
            _match_value(play_state.get("PlaySessionId"), normalized_play_session_id),
            _match_value(session.get("AccessToken"), normalized_token),
        )
        return any(candidates)

    matched_sessions = [session for session in sessions if _session_matches(session)]
    if not matched_sessions:
        return None

    # 优先使用当前确实处于播放态的会话
    for session in matched_sessions:
        if session.get("NowPlayingItem"):
            return session
    return matched_sessions[0]


def identity_is_corroborated(
    sessions: List[Dict[str, Any]],
    resolved_user_id: str,
    *,
    token: str = "",
    session_id: str = "",
    play_session_id: str = "",
) -> bool:
    """判断 resolved_user_id 是否被"密钥型"线索证实。

    `userId` 与 `deviceId` 都可以被任意请求方构造（前者来自 query，后者来自请求头），
    仅凭它们无法证明"发起请求的人就是该用户"——否则任何人都能把线路违规栽给他人。
    只有 token / sessionId / playSessionId 这类密钥才会与活跃会话绑定，
    因此要求：存在一个会话，其 UserId 等于解析结果，且其 AccessToken/Id/PlaySessionId
    与请求携带的密钥一致。

    :return: True 表示身份已被证实，可以据此执行封禁/下发停止指令等强制动作
    """
    if not resolved_user_id:
        return False
    if not any([token, session_id, play_session_id]):
        return False

    for session in sessions:
        if normalize_identifier(session.get("UserId")) != resolved_user_id:
            continue
        play_state = session.get("PlayState") or {}
        if token and normalize_identifier(session.get("AccessToken")) == token:
            return True
        if session_id and normalize_identifier(session.get("Id")) == session_id:
            return True
        if play_session_id and (
            normalize_identifier(session.get("PlaySessionId")) == play_session_id
            or normalize_identifier(play_state.get("PlaySessionId")) == play_session_id
        ):
            return True
    return False


async def resolve_user_context(
    *,
    user_id: str = "",
    device_id: str = "",
    session_id: str = "",
    play_session_id: str = "",
    token: str = "",
    auth_header: str = "",
    original_request_uri: str = "",
) -> Tuple[str, Optional[Dict[str, Any]], str, bool]:
    """从 userId / 认证头 / 活跃会话中尽量反查用户上下文

    :return: (resolved_user_id, matched_session, resolved_from, identity_verified)
             identity_verified 为 True 时才允许执行强制动作（下发停止指令/封禁）
    """
    auth_info = parse_emby_authorization(auth_header)
    original_query = parse_original_request_uri(original_request_uri)
    resolved_from = ""

    direct_user_id = normalize_identifier(user_id)
    auth_user_id = normalize_identifier(auth_info.get("UserId"))
    original_query_user_id = normalize_identifier(original_query.get("userId"))
    direct_user_exists = bool(sql_get_emby(direct_user_id)) if direct_user_id else False
    auth_user_exists = bool(sql_get_emby(auth_user_id)) if auth_user_id else False
    original_query_user_exists = bool(sql_get_emby(original_query_user_id)) if original_query_user_id else False

    if direct_user_id and direct_user_exists:
        resolved_user_id = direct_user_id
        resolved_from = "query.userId"
    elif auth_user_id and auth_user_exists:
        resolved_user_id = auth_user_id
        resolved_from = "header.X-Emby-Authorization.UserId"
    elif original_query_user_id and original_query_user_exists:
        resolved_user_id = original_query_user_id
        resolved_from = "header.X-Original-URI.query.userId"
    else:
        resolved_user_id = auth_user_id or original_query_user_id or direct_user_id

    resolved_device_id = (
        normalize_identifier(device_id)
        or normalize_identifier(auth_info.get("DeviceId"))
        or normalize_identifier(original_query.get("X-Emby-Device-Id"))
        or normalize_identifier(original_query.get("DeviceId"))
    )
    resolved_session_id = normalize_identifier(session_id) or normalize_identifier(original_query.get("SessionId"))
    resolved_play_session_id = (
        normalize_identifier(play_session_id)
        or normalize_identifier(original_query.get("PlaySessionId"))
    )
    resolved_token = (
        normalize_identifier(token)
        or normalize_identifier(auth_info.get("Token"))
        or normalize_identifier(original_query.get("X-Emby-Token"))
        or normalize_identifier(original_query.get("api_key"))
    )

    if resolved_user_id and not any([resolved_device_id, resolved_session_id, resolved_play_session_id, resolved_token]):
        # 只有 query 里的 userId 可用：不足以证实身份
        return resolved_user_id, None, resolved_from, False

    sessions = await fetch_active_sessions()
    if not sessions:
        if resolved_user_id and not resolved_from:
            resolved_from = "derived.before_session_lookup"
        return resolved_user_id, None, resolved_from, False

    matched_session = find_matching_session(
        sessions,
        user_id=resolved_user_id,
        device_id=resolved_device_id,
        session_id=resolved_session_id,
        play_session_id=resolved_play_session_id,
        token=resolved_token,
    )

    # 如果 query 里的 userId 明显是错的，不要让它阻断 token / device / session 的正确匹配。
    if (
        (not matched_session or (direct_user_id and not direct_user_exists))
        and any([resolved_device_id, resolved_session_id, resolved_play_session_id, resolved_token])
    ):
        retry_session = find_matching_session(
            sessions,
            user_id="",
            device_id=resolved_device_id,
            session_id=resolved_session_id,
            play_session_id=resolved_play_session_id,
            token=resolved_token,
        )
        if retry_session:
            matched_session = retry_session

    if matched_session and not resolved_user_id:
        resolved_user_id = normalize_identifier(matched_session.get("UserId"))
        if normalize_identifier(matched_session.get("AccessToken")) == resolved_token and resolved_token:
            resolved_from = "emby.sessions.AccessToken"
        elif normalize_identifier(matched_session.get("DeviceId")) == resolved_device_id and resolved_device_id:
            resolved_from = "emby.sessions.DeviceId"
        elif normalize_identifier(matched_session.get("Id")) == resolved_session_id and resolved_session_id:
            resolved_from = "emby.sessions.Id"
        elif (
            normalize_identifier(matched_session.get("PlaySessionId")) == resolved_play_session_id
            or normalize_identifier((matched_session.get("PlayState") or {}).get("PlaySessionId")) == resolved_play_session_id
        ) and resolved_play_session_id:
            resolved_from = "emby.sessions.PlaySessionId"
        else:
            resolved_from = "emby.sessions.fallback"
    elif matched_session and direct_user_id and not direct_user_exists:
        session_user_id = normalize_identifier(matched_session.get("UserId"))
        if session_user_id and session_user_id != direct_user_id:
            resolved_user_id = session_user_id
            if normalize_identifier(matched_session.get("AccessToken")) == resolved_token and resolved_token:
                resolved_from = "emby.sessions.AccessToken.override_bad_query_userId"
            elif normalize_identifier(matched_session.get("DeviceId")) == resolved_device_id and resolved_device_id:
                resolved_from = "emby.sessions.DeviceId.override_bad_query_userId"
            elif normalize_identifier(matched_session.get("Id")) == resolved_session_id and resolved_session_id:
                resolved_from = "emby.sessions.Id.override_bad_query_userId"
            elif (
                normalize_identifier(matched_session.get("PlaySessionId")) == resolved_play_session_id
                or normalize_identifier((matched_session.get("PlayState") or {}).get("PlaySessionId")) == resolved_play_session_id
            ) and resolved_play_session_id:
                resolved_from = "emby.sessions.PlaySessionId.override_bad_query_userId"
            else:
                resolved_from = "emby.sessions.fallback.override_bad_query_userId"

    # 身份证实：必须由"密钥型"线索（token / sessionId / playSessionId）与活跃会话匹配，
    # 且该会话的 UserId 与解析出的用户一致。
    # 仅凭 query.userId 或 deviceId 命中的身份只用于展示与告警，不能据此封禁/下发停止指令。
    identity_verified = identity_is_corroborated(
        sessions,
        resolved_user_id,
        token=resolved_token,
        session_id=resolved_session_id,
        play_session_id=resolved_play_session_id,
    )

    return resolved_user_id, matched_session, resolved_from, identity_verified


async def log_line_violation(
    user_id: str = None,
    user_name: str = None,
    session_id: str = None,
    client_name: str = None,
    tg_id: int = None,
    user_lv: str = None,
    action_taken: str = None,
):
    """记录线路权限违规"""
    try:
        lv_display = {'a': '白名单', 'b': '普通用户', 'c': '封禁用户', 'd': '未注册'}.get(user_lv, '未知')

        log_message = (
            f"⚠️ 线路权限违规\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 用户: {user_name or 'Unknown'}\n"
            f"🆔 Emby ID: {user_id or 'Unknown'}\n"
            f"📱 TG ID: {f'[{tg_id}](tg://user?id={tg_id})' if tg_id else 'Unknown'}\n"
            f"🏷️ 用户等级: {lv_display}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📺 客户端: {client_name or 'Unknown'}\n"
            f"🔑 会话ID: {session_id or 'Unknown'}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚨 处理措施: {action_taken or '无'}\n"
            f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        LOGGER.warning(log_message)

        if hasattr(config, "group") and config.group:
            try:
                out = await bot.send_message(chat_id=config.group[0], text=log_message)
                if tg_id:
                    await out.forward(tg_id)
            except Exception as e:
                LOGGER.error(f"发送线路违规通知失败: {str(e)}")

    except Exception as e:
        LOGGER.error(f"记录线路违规失败: {str(e)}")


async def handle_line_violation(
    emby_id: str,
    user_name: str,
    session_id: str,
    client_name: str,
    user_details: Emby,
) -> dict:
    """
    处理线路权限违规
    :return: 处理结果字典
    """
    action_taken_list = []
    block_success = False
    # 语义：停止指令是否被服务端**受理**，不是「流是否已经断了」。
    terminate_accepted = False

    terminate_session_enabled = getattr(config, "line_filter_terminate_session", True)
    block_user_enabled = getattr(config, "line_filter_block_user", False)

    if terminate_session_enabled:
        if not session_id:
            # 没匹配到会话时 session_id 是空串，f-string 会拼出
            # /emby/Sessions//Playing/Stop —— 无意义且必然无效的请求。
            # 跳过并留下线索，不要发出去，也不要假装做过什么。
            action_taken_list.append("⏭️ 未匹配到会话，跳过停止指令")
            LOGGER.warning("未匹配到会话，跳过下发停止指令")
        else:
            reason = "您使用的线路与您的账户等级不匹配，请使用正确的线路"
            # terminate_session() 返回 True 只代表服务端**受理**了停止指令：它是向
            # 客户端下发的远程控制指令，这台 Emby 上的客户端群体基本不支持远程控制
            # （实测 204 之后 30 秒仍在播）。真正能停流的是把用户策略 IsDisabled
            # 置真（下面 block_user_enabled 那条路径）。所以这里只敢说「已下发」。
            terminate_accepted = await emby.terminate_session(session_id, reason)
            if terminate_accepted:
                action_taken_list.append("⏳ 已下发停止指令（待确认）")
                LOGGER.info(f"已下发停止指令（服务端已受理，不代表已断流）{session_id}")
            else:
                action_taken_list.append("❌ 下发停止指令失败")
                LOGGER.error(f"下发停止指令失败 {session_id}")

    if block_user_enabled:
        block_success = await emby.emby_change_policy(emby_id=emby_id, disable=True)
        if block_success:
            if user_details:
                sql_update_emby(Emby.tg == user_details.tg, lv="c")
            action_taken_list.append("✅ 已封禁用户")
            LOGGER.info(f"成功封禁违规用户 {emby_id}")
        else:
            action_taken_list.append("❌ 封禁用户失败")
            LOGGER.error(f"封禁违规用户失败 {emby_id}")

    action_taken = " | ".join(action_taken_list) if action_taken_list else "仅记录，未采取行动"
    # 这段文案既发管理群、也会被 log_line_violation forward 给用户本人，所以不能只按
    # 管理员口径写：必须让用户也明白「指令已下发 ≠ 已经断流」，否则就是又一次谎报。
    if terminate_accepted:
        action_taken += (
            "\nℹ️ 说明：「已下发停止指令」表示服务端受理了停止请求；"
            "若你的播放器不支持远程控制，播放可能不会立即停止，请手动停止播放。"
        )

    await log_line_violation(
        user_id=emby_id,
        user_name=user_name,
        session_id=session_id,
        client_name=client_name,
        tg_id=user_details.tg if user_details else None,
        user_lv=user_details.lv if user_details else None,
        action_taken=action_taken,
    )

    return {
        # ⚠️ 字段名保持 "terminate_success" 不变：已有外部调用方按这个名字取值，
        # 改名属于破坏性契约变更。但它的**语义**是「停止指令是否被服务端受理」，
        # **不是**「流是否已经断了」——`POST /Sessions/{Id}/Playing/Stop` 只是向
        # 客户端下发远程控制指令，这台 Emby 上的客户端群体基本不支持远程控制
        # （实测 204 之后 30 秒仍在播）。调用方不要用它判断「已经停止播放」；
        # 真正能停流的是用户策略 IsDisabled（见 block_success / emby_change_policy）。
        "terminate_success": terminate_accepted,
        "block_success": block_success,
        "action_taken": action_taken,
    }


@router.get("/line_report")
async def line_report(
    userId: str = "",
    line: str = "",
    host: str = "",
    deviceId: str = "",
    sessionId: str = "",
    playSessionId: str = "",
    token: str = "",
    x_emby_authorization: Optional[str] = Header(default=None, alias="X-Emby-Authorization"),
    x_emby_token: Optional[str] = Header(default=None, alias="X-Emby-Token"),
    x_original_uri: Optional[str] = Header(default=None, alias="X-Original-URI"),
):
    """
    接收 nginx mirror 转发的线路访问通知。

    nginx 在代理播放请求时，通过 mirror 将 userId、line（线路标识）、host（域名）
    转发到此端点。Bot 据此判断用户是否有权使用该线路。

    :param userId: Emby 用户 ID（从 nginx $arg_userId 获取）
    :param line: 线路标识名称（在 nginx 中通过 set $line_name 定义）
    :param host: 用户访问的域名（从 nginx $server_name 获取）
    :param deviceId: 客户端设备 ID（建议从 nginx $arg_X_Emby_Device_Id 转发）
    :param sessionId: Emby 会话 ID（如能获取建议转发）
    :param playSessionId: Emby 播放会话 ID（如能获取建议转发）
    """
    if not line:
        return {"status": "ignored", "message": "Missing line"}

    # 检查是否配置了白名单线路
    whitelist_line = getattr(config, "emby_whitelist_line", None)
    if not whitelist_line:
        return {"status": "skipped", "message": "No whitelist line configured"}

    # 廉价前置校验：没有任何可用于识别会话的线索时直接忽略，
    # 避免对无意义的请求做数据库查询与 Emby 会话拉取（减少放大面）。
    if not any([userId, deviceId, sessionId, playSessionId, token, x_emby_token, x_emby_authorization]):
        return {"status": "ignored", "message": "No identity hints"}

    redacted_original_request_uri = redact_request_uri(x_original_uri or "")
    resolved_user_id, matched_session, resolved_from, identity_verified = await resolve_user_context(
        user_id=userId,
        device_id=deviceId,
        session_id=sessionId,
        play_session_id=playSessionId,
        token=token or (x_emby_token or ""),
        auth_header=x_emby_authorization or "",
        original_request_uri=x_original_uri or "",
    )

    if not resolved_user_id:
        LOGGER.warning(
            "线路上报忽略: 无法识别用户 "
            f"(line={line}, host={host}, deviceId={deviceId}, sessionId={sessionId}, "
            f"playSessionId={playSessionId}, x_original_uri={redacted_original_request_uri or '<empty>'})"
        )
        return {
            "status": "ignored",
            "message": "Missing user identity",
            "line": line,
            "host": host,
            "deviceId": deviceId,
            "resolved_from": resolved_from,
        }

    # 构造 server_address 用于线路判断
    server_address = host or line

    # 获取用户详情
    user_details = sql_get_emby(resolved_user_id)

    # 白名单用户可以用任何线路
    if is_user_whitelisted(user_details):
        LOGGER.debug(f"线路检查通过: 白名单用户 {resolved_user_id} 使用线路 {line}")
        return {"status": "allowed", "message": "Whitelist user"}

    # 检查是否使用白名单线路
    using_whitelist = is_whitelist_line(server_address)

    if using_whitelist:
        # 身份未证实时不执行任何强制动作：
        # query.userId / deviceId 都可被请求方任意构造，据此封禁等于把违规栽给他人。
        if not identity_verified:
            LOGGER.warning(
                f"线路权限疑似违规但身份无法证实，仅记录不处置: "
                f"claimed_user={resolved_user_id}, resolved_from={resolved_from}, "
                f"line={line}, host={host}"
            )
            return {
                "status": "unverified",
                "message": "Violation reported but identity not corroborated; no action taken",
                "line": line,
                "host": host,
                "userId": resolved_user_id,
                "resolved_from": resolved_from,
            }

        # 冷却期内的重复上报直接忽略（正因客户端不响应停止指令，它才会持续上报）
        if is_in_cooldown(resolved_user_id):
            cooldown_seconds = getattr(config, "line_filter_cooldown_seconds", 60)
            LOGGER.debug(
                f"线路违规冷却中，忽略重复上报: 用户 {resolved_user_id} "
                f"(冷却 {cooldown_seconds}s 内)"
            )
            return {
                "status": "cooldown",
                "message": "Violation already handled, in cooldown",
                "userId": resolved_user_id,
            }

        update_cooldown(resolved_user_id)

        LOGGER.warning(
            f"线路权限违规(nginx): 用户 {resolved_user_id} 通过 {server_address} 使用白名单线路"
        )

        # 查询活跃会话以获取 session_id 等信息
        session = matched_session
        if not session:
            sessions = await fetch_active_sessions()
            session = find_matching_session(
                sessions,
                user_id=resolved_user_id,
                device_id=deviceId,
                session_id=sessionId,
                play_session_id=playSessionId,
                token=token or (x_emby_token or ""),
            )

        session_id = normalize_identifier(session.get("Id")) if session else ""
        client_name = normalize_identifier(session.get("Client")) if session else ""
        user_name = normalize_identifier(session.get("UserName")) if session else ""

        result = await handle_line_violation(
            emby_id=resolved_user_id,
            user_name=user_name or (user_details.name if user_details else ""),
            session_id=session_id,
            client_name=client_name,
            user_details=user_details,
        )

        return {
            "status": "blocked",
            "message": "Line not allowed",
            "line": line,
            "host": host,
            "userId": resolved_user_id,
            "action_result": result,
        }

    LOGGER.debug(f"线路检查通过: 用户 {resolved_user_id} 使用线路 {line}")
    return {"status": "allowed", "line": line, "host": host, "userId": resolved_user_id}
