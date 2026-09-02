"""
同时播放限制检测模块

功能：
- 定时检测 Emby 活跃会话
- 当用户同时播放流超过限制时，警告并终止所有流
- 在群内通报违规事件
- 超过警告次数自动封禁账号
- 可选对接 Tracearr 进行流终止

Author: embyboss
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from bot import bot, group, config, LOGGER
from bot.func_helper.emby import emby
from bot.func_helper.msg_utils import sendMessage
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby


def _now_str():
    """返回当前北京时间字符串"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


async def get_sessions_by_user():
    """
    获取所有活跃会话，按 Emby 用户ID 分组
    :return: {emby_user_id: [session, ...], ...}
    """
    result = await emby._request("GET", "/emby/Sessions")
    if not result.success or not result.data:
        return {}

    sessions_by_user = defaultdict(list)
    for session in result.data:
        user_id = session.get("UserId")
        if not user_id:
            continue
        # 只统计正在播放的会话
        if session.get("NowPlayingItem"):
            sessions_by_user[user_id].append(session)

    return sessions_by_user


async def terminate_all_user_sessions(emby_user_id: str, sessions: list, reason: str = "同时播放超出限制"):
    """
    终止某用户的所有播放会话
    :param emby_user_id: Emby 用户ID
    :param sessions: 会话列表
    :param reason: 终止原因
    :return: (成功数, 失败数)
    """
    success_count = 0
    fail_count = 0

    for session in sessions:
        session_id = session.get("Id")
        if not session_id:
            continue

        # 先发送消息通知用户
        message_data = {
            "Text": f"🚫 {reason}，您的所有播放流已被终止。",
            "Header": "播放限制警告",
            "TimeoutMs": 10000
        }
        await emby._request('POST', f'/emby/Sessions/{session_id}/Message', json=message_data)

        # 停止播放
        stop_result = await emby._request('POST', f'/emby/Sessions/{session_id}/Playing/Stop')
        if stop_result.success:
            success_count += 1
        else:
            fail_count += 1
            LOGGER.warning(f"终止会话失败: session={session_id}, user={emby_user_id}, error={stop_result.error}")

    return success_count, fail_count


async def send_group_announcement(text: str):
    """
    在群内发送通报
    :param text: 通报内容
    """
    if not group:
        return
    try:
        await sendMessage(None, text, send=True, chat_id=group[0])
    except Exception as e:
        LOGGER.error(f"群内通报发送失败: {e}")


async def warn_user(tg_id: int, text: str):
    """
    向用户发送警告消息
    :param tg_id: Telegram 用户ID
    :param text: 警告内容
    """
    try:
        await bot.send_message(chat_id=tg_id, text=text)
    except Exception as e:
        LOGGER.error(f"发送警告消息失败: tg={tg_id}, error={e}")


async def ban_user(emby_id: str, tg_id: int = None):
    """
    封禁用户（禁用 Emby 账号）
    :param emby_id: Emby 用户ID
    :param tg_id: Telegram 用户ID（可选）
    """
    try:
        result = await emby.emby_change_policy(emby_id, admin=False, disable=True)
        if result:
            LOGGER.info(f"已封禁用户: emby_id={emby_id}, tg_id={tg_id}")
        else:
            LOGGER.error(f"封禁用户失败: emby_id={emby_id}")
        return result
    except Exception as e:
        LOGGER.error(f"封禁用户异常: emby_id={emby_id}, error={e}")
        return False


async def check_concurrent_play_limit():
    """
    检测同时播放限制的主函数
    定时调用，检查所有用户的并发播放数量
    """
    if not config.concurrent_play_limit_enabled:
        return

    limit = config.concurrent_play_limit
    warn_threshold = config.concurrent_play_warn_threshold

    try:
        sessions_by_user = await get_sessions_by_user()
    except Exception as e:
        LOGGER.error(f"获取会话列表失败: {e}")
        return

    for emby_user_id, sessions in sessions_by_user.items():
        stream_count = len(sessions)
        if stream_count <= limit:
            continue

        # 用户超出了播放限制
        # 查找对应的数据库记录
        e = sql_get_emby(tg=emby_user_id)
        if e is None or e.embyid is None:
            # 尝试通过 embyid 查找
            e = sql_get_by_embyid(emby_user_id)
        
        if e is None:
            LOGGER.warning(f"未找到 emby_user_id={emby_user_id} 的数据库记录，跳过")
            continue

        user_name = e.name or "未知用户"
        tg_id = e.tg
        current_warns = e.concurrent_warn_count or 0

        # 构建违规信息
        now_str = _now_str()
        violation_msg = (
            f"⚠️ **同时播放限制警告**\n\n"
            f"用户: `{user_name}` (TG: `{tg_id}`)\n"
            f"Emby ID: `{emby_user_id}`\n"
            f"当前播放流: **{stream_count}** 个 (限制: **{limit}** 个)\n"
            f"检测时间: {now_str}\n"
            f"累计警告: **{current_warns + 1}** / **{warn_threshold}** 次\n"
        )

        # 列出正在播放的内容
        for idx, session in enumerate(sessions, 1):
            now_playing = session.get("NowPlayingItem", {})
            media_name = now_playing.get("Name", "未知")
            client_name = session.get("Client", "未知设备")
            violation_msg += f"  {idx}. 🎬 `{media_name}` | 📱 {client_name}\n"

        # 终止所有流
        success, fail = await terminate_all_user_sessions(
            emby_user_id, sessions,
            reason=f"同时播放超出限制({stream_count}/{limit})"
        )
        violation_msg += f"\n✅ 已终止: {success} 个流"
        if fail > 0:
            violation_msg += f" | ❌ 失败: {fail} 个流"

        # 更新警告计数
        new_warn_count = current_warns + 1
        sql_update_emby(Emby.tg == tg_id, concurrent_warn_count=new_warn_count)

        # 判断是否超过警告阈值
        if new_warn_count >= warn_threshold:
            # 封禁用户
            banned = await ban_user(emby_user_id, tg_id)
            if banned:
                violation_msg += f"\n\n🔴 **警告次数已超限 ({new_warn_count}/{warn_threshold})，账号已被自动封禁！**"
            else:
                violation_msg += f"\n\n🔴 **警告次数已超限 ({new_warn_count}/{warn_threshold})，封禁失败，请手动处理！**"
        else:
            violation_msg += f"\n\n⚠️ 再犯 **{warn_threshold - new_warn_count}** 次将自动封禁账号！"

        # 向用户发送警告
        if tg_id:
            user_warn_msg = (
                f"🚫 **播放限制警告**\n\n"
                f"您的账号当前有 **{stream_count}** 个播放流，超出限制 **{limit}** 个。\n"
                f"所有播放流已被强制终止。\n"
                f"警告次数: **{new_warn_count}** / **{warn_threshold}**\n\n"
                f"⚠️ 超过 {warn_threshold} 次将自动封禁账号！"
            )
            await warn_user(tg_id, user_warn_msg)

        # 群内通报
        await send_group_announcement(violation_msg)
        LOGGER.info(f"同时播放限制: user={user_name}, streams={stream_count}, warns={new_warn_count}")


def sql_get_by_embyid(embyid: str):
    """
    通过 embyid 查询数据库记录
    :param embyid: Emby 用户ID
    :return: Emby 记录或 None
    """
    from bot.sql_helper import Session
    from bot.sql_helper.sql_emby import Emby as EmbyModel
    with Session() as session:
        try:
            return session.query(EmbyModel).filter(EmbyModel.embyid == embyid).first()
        except:
            return None


async def reset_all_warn_counts():
    """
    重置所有用户的警告计数（可用于定期清零）
    """
    from bot.sql_helper import Session
    from bot.sql_helper.sql_emby import Emby as EmbyModel
    with Session() as session:
        try:
            session.query(EmbyModel).update({EmbyModel.concurrent_warn_count: 0})
            session.commit()
            LOGGER.info("已重置所有用户的同时播放警告计数")
            return True
        except Exception as e:
            LOGGER.error(f"重置警告计数失败: {e}")
            session.rollback()
            return False
