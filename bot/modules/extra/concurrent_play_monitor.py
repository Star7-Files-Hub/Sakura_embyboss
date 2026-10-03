"""
同时播放限制检测模块

功能：
- 定时检测 Emby 活跃会话
- 当用户同时播放流超过限制时，警告并终止所有流
- 在群内通报违规事件
- 超过警告次数自动封禁账号

判定规则（按优先级）：
1. bot 管理员 —— **永久豁免**，没有开关可改（避免管理员把自己锁死）。
   判定统一走 judge_admins()，即「站长 owner + config.admins」，与面板/命令权限一致
2. 白名单 lv='a' —— 默认豁免；开启 concurrent_play_limit_whitelist_enabled 后
   按 concurrent_play_limit_whitelist 这个独立上限判定
3. 其他用户 —— 按 concurrent_play_limit 判定
4. 不在数据库中的 Emby 账号 —— 跳过并记 warning（无法告警/封禁）

Author: embyboss
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from bot import bot, group, config, LOGGER
from bot.func_helper.emby import emby
# 模块本身也要引用：is_emby_admin() 是模块级函数（emby 单例上没有它）
from bot.func_helper import emby as emby_mod
from bot.func_helper.msg_utils import sendMessage
from bot.func_helper.utils import judge_admins
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


async def _notify_session(session_id: str, text: str):
    """向 Emby 客户端推送弹窗通知。"""
    await emby._request('POST', f'/emby/Sessions/{session_id}/Message', json={
        "Text": text,
        "Header": "播放限制警告",
        "TimeoutMs": 10000,
    })


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

        # 停止播放
        stop_result = await emby._request('POST', f'/emby/Sessions/{session_id}/Playing/Stop')
        if stop_result.success:
            success_count += 1
            # 通知放在停止成功之后：避免"弹窗说已终止、实际没停掉"
            await _notify_session(session_id, f"🚫 {reason}，您的播放流已被终止。")
            continue

        LOGGER.warning(f"终止会话失败: session={session_id}, user={emby_user_id}, error={stop_result.error}")

        fail_count += 1

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

    default_limit = config.concurrent_play_limit
    whitelist_limit = config.concurrent_play_limit_whitelist
    whitelist_enforced = config.concurrent_play_limit_whitelist_enabled
    warn_threshold = config.concurrent_play_warn_threshold

    # 粗筛：用"可能生效的上限"里**最小**的那个过滤，保持零查库快路径。
    #
    # 这里必须是 min 而不是 max：任何应当被处罚的用户，其流数必然 > 自己适用的上限，
    # 而适用上限 ≥ min(...)，所以流数 > min 一定成立 —— 不会漏判。
    # 反过来若用 max，普通用户(上限2)播 3 个流时会 ≤ max(2,4)=4 而被直接跳过，
    # 连查库都发生不了，永远不会被处罚（这是实测抓到的真实 bug）。
    # 精确判定仍要等查到用户记录、知道是不是白名单之后再做。
    prefilter = min(default_limit, whitelist_limit) if whitelist_enforced else default_limit

    try:
        sessions_by_user = await get_sessions_by_user()
    except Exception as e:
        LOGGER.error(f"获取会话列表失败: {e}")
        return

    for emby_user_id, sessions in sessions_by_user.items():
        stream_count = len(sessions)
        if stream_count <= prefilter:
            continue

        # 用户超出了播放限制
        # 查找对应的数据库记录
        e = sql_get_emby(tg=emby_user_id)
        if e is None or e.embyid is None:
            # 尝试通过 embyid 查找
            e = sql_get_by_embyid(emby_user_id)
        
        if e is None:
            # 不在数据库中的 Emby 用户（例如直接建在 Emby 侧、或管理员账号）
            # 无法告警/封禁，这里只记录以便运维发现。
            # 注意：这类账号会永久绕过并发限制，建议为其在 bot 中建档或设为白名单。
            session_user = ""
            if sessions:
                session_user = sessions[0].get("UserName") or ""
            LOGGER.warning(
                f"未找到 emby_user_id={emby_user_id} (UserName={session_user or '未知'}) "
                f"的数据库记录，跳过并发限制检查（该账号不受限制）"
            )
            continue

        # 1) bot 管理员**永久**豁免，没有开关可以改变这一点（避免管理员把自己锁死）。
        #    这一条必须排在白名单判断之前：既是管理员又是白名单的账号也应当豁免。
        #    统一走 judge_admins()：它把站长 owner 与 config.admins 都算作管理员，
        #    与面板/命令的权限判定共用同一套定义（原先只查 config.admins，owner 不在
        #    豁免范围内，与 filters.admins_on_filter 不一致）。
        if e.tg is not None and judge_admins(e.tg):
            LOGGER.debug(
                f"跳过 bot 管理员的并发播放检查: emby_user_id={emby_user_id}, tg={e.tg}"
            )
            continue

        # 1b) **Emby 侧**管理员同样永久豁免（用户明确要求）。
        #     不能只依赖上面那条「查不到 bot 记录就跳过」——那只覆盖了没建档的管理员。
        #     若管理员同时在 bot 里建了档（如 lv='b'），超限就会走到下面的封禁；
        #     而封禁在官方 Emby 与 go-emby 上**都会把 IsAdministrator 写成 false**
        #     （create_policy(admin=False) / PUT /admin/users 的 Admin=false），
        #     等于把管理员**永久降权**。go-emby 适配后该动作从「空转」变成「真的生效」。
        #     取不到身份（None）时**同样豁免**：宁可少限制一个用户一个周期，也不能
        #     误伤管理员；异常会打 WARNING，便于运维发现。
        emby_admin = await emby_mod.is_emby_admin(emby_user_id)
        if emby_admin is None or emby_admin:
            LOGGER.warning(
                f"跳过 Emby 管理员的并发播放检查: emby_user_id={emby_user_id}, tg={e.tg}, "
                f"判定={'未知(按豁免处理)' if emby_admin is None else '是管理员'}"
            )
            continue

        is_whitelist = (e.lv or "").lower() == "a"

        # 2) 白名单（lv='a'）默认豁免；开启 concurrent_play_limit_whitelist_enabled
        #    后改为按 concurrent_play_limit_whitelist 这个独立上限判定。
        if is_whitelist and not whitelist_enforced:
            LOGGER.debug(
                f"跳过白名单账号的并发播放检查: emby_user_id={emby_user_id}, tg={e.tg}"
            )
            continue

        # 3) 确定该用户适用的上限后再做精确比较（粗筛可能放过了未超限的用户）
        user_limit = whitelist_limit if is_whitelist else default_limit
        if stream_count <= user_limit:
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
            f"当前播放流: **{stream_count}** 个 (限制: **{user_limit}** 个)\n"
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
            reason=f"同时播放超出限制({stream_count}/{user_limit})"
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

        # 向用户发送警告（文案按实际终止结果生成，避免"没停掉却说已终止"）
        if tg_id:
            if fail == 0:
                enforce_line = "所有播放流已被强制终止。"
            elif success == 0:
                enforce_line = (
                    "⚠️ 播放流终止失败，请**立即手动停止播放**，否则将直接封禁账号。"
                )
            else:
                enforce_line = (
                    f"⚠️ 已终止 {success} 个流，另有 {fail} 个流终止失败，"
                    f"请**立即手动停止播放**，否则将直接封禁账号。"
                )
            user_warn_msg = (
                f"🚫 **播放限制警告**\n\n"
                f"您的账号当前有 **{stream_count}** 个播放流，超出限制 **{user_limit}** 个。\n"
                f"{enforce_line}\n"
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
