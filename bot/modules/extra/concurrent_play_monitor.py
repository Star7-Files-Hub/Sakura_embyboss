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

# 停止指令下发后的延迟复验等待秒数。
#
# 为什么需要它：Emby 的 `POST /Sessions/{id}/Playing/Stop` 返回 204 只代表**服务端接受了
# 指令**，客户端真正断流有明显延迟。2026-10-04 在 ChaPanda 的 Emby 4.10 上实测 toe 账号：
# 13:55:42.9 / 13:55:43.2 两条 Stop 都返回 204，但客户端直到 13:56:06.8 / 13:56:09.9
# 才上报 Playback stopped（延迟 23.9s / 26.7s）。若当场就宣称"已终止 N 个流"，群里看到的
# 数字会与 Emby 活动页自相矛盾（活动页里流还挂着），属于误导性文案。
_STOP_VERIFY_FOLLOWUP = 30

# 异步复验任务的强引用集合。asyncio.create_task 的返回值若不持有引用，
# 任务可能在执行途中被 GC 回收（Python 官方文档明确提示）。
_VERIFY_TASKS = set()


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


async def _sessions_still_playing(session_ids) -> set:
    """
    重新拉一次会话列表，返回其中**仍在播放**的 session id 集合。

    :param session_ids: 需要复验的 session id 集合
    :return: set（可能为空）；**取不到会话列表时返回 None**，表示"无法确认"，
             调用方据此避免把"查不到"当成"已经停掉"来谎报成功。
    """
    try:
        result = await emby._request("GET", "/emby/Sessions")
    except Exception as e:
        LOGGER.warning(f"复验会话状态失败（按无法确认处理）: {type(e).__name__}: {e}")
        return None
    if not result.success or not isinstance(result.data, list):
        LOGGER.warning(
            f"复验会话状态失败（按无法确认处理）: {getattr(result, 'error', None) or '返回不是列表'}"
        )
        return None

    still = set()
    for s in result.data:
        sid = s.get("Id")
        if sid in session_ids and s.get("NowPlayingItem"):
            still.add(sid)
    return still


async def _verify_stop_followup(session_ids, user_name, wait: int = None):
    """
    延迟复验：确认这些会话是否**真的**断流，并把实测结果补发到群里。

    单独补发而不是当场下结论的原因见模块顶部 _STOP_VERIFY_FOLLOWUP 的说明：
    Stop 返回 204 时客户端往往还在播，当场宣称"已终止"会与 Emby 活动页矛盾。
    """
    wait = _STOP_VERIFY_FOLLOWUP if wait is None else wait
    try:
        await asyncio.sleep(wait)
        total = len(session_ids)
        still = await _sessions_still_playing(set(session_ids))

        if still is None:
            await send_group_announcement(
                f"🔎 **终止复验**\n用户: `{user_name}`\n"
                f"⚠️ 无法获取会话状态，**未能确认**是否已停止，请到 Emby 活动页核对。"
            )
            LOGGER.warning(f"终止复验: user={user_name}, 结果=无法确认")
            return

        stopped = total - len(still)
        if not still:
            await send_group_announcement(
                f"🔎 **终止复验**\n用户: `{user_name}`\n"
                f"✅ 已确认全部断开: **{stopped}/{total}** 个流。"
            )
        else:
            await send_group_announcement(
                f"🔎 **终止复验**\n用户: `{user_name}`\n"
                f"✅ 已断开: **{stopped}** 个 | ⚠️ {wait} 秒后仍在播放: **{len(still)}** 个\n"
                f"请手动处理仍在播放的会话。"
            )
        LOGGER.info(
            f"终止复验: user={user_name}, 已断开={stopped}, 仍在播放={len(still)}, 等待={wait}s"
        )
    except Exception as e:
        LOGGER.error(f"终止复验失败: user={user_name}, error={type(e).__name__}: {e}")


def _schedule_stop_verify(session_ids, user_name):
    """启动异步复验任务，并持有强引用避免被 GC 回收。"""
    task = asyncio.create_task(_verify_stop_followup(session_ids, user_name))
    _VERIFY_TASKS.add(task)
    task.add_done_callback(_VERIFY_TASKS.discard)
    return task


async def terminate_all_user_sessions(emby_user_id: str, sessions: list, reason: str = "同时播放超出限制"):
    """
    终止某用户的所有播放会话

    :param emby_user_id: Emby 用户ID
    :param sessions: 会话列表
    :param reason: 终止原因
    :return: (accepted, rejected, accepted_ids)
        accepted     - 服务端**接受**停止指令的会话数（HTTP 2xx）。
                       注意这**不代表客户端已断流**，真实结果要靠 _verify_stop_followup 复验
        rejected     - 服务端**拒绝**的会话数（非 2xx）
        accepted_ids - 被接受的 session id 列表，供调用方做延迟复验
    """
    accepted = 0
    rejected = 0
    accepted_ids = []

    for session in sessions:
        session_id = session.get("Id")
        if not session_id:
            continue

        # 停止播放
        stop_result = await emby._request('POST', f'/emby/Sessions/{session_id}/Playing/Stop')
        if stop_result.success:
            accepted += 1
            accepted_ids.append(session_id)
            # 通知放在停止指令被接受之后：避免"弹窗说已终止、实际没下发"
            await _notify_session(session_id, f"🚫 {reason}，您的播放流已被终止。")
            continue

        LOGGER.warning(f"终止会话失败: session={session_id}, user={emby_user_id}, error={stop_result.error}")

        rejected += 1

    return accepted, rejected, accepted_ids


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
        #     而封禁会把 `IsAdministrator` 写成 false（create_policy(admin=False)），
        #     等于把管理员**永久降权**。
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

        # 终止所有流。
        # 注意：这里拿到的是"服务端是否**接受**了停止指令"，不是"客户端是否已断流"。
        # Emby 的 Stop 返回 204 后客户端可能还要 20~30 秒才真正断开（2026-10-04 实测
        # 23.9s/26.7s），所以文案必须区分「已下发」与「已确认停止」，
        # 真实结果由 _verify_stop_followup 在延迟后补发，绝不在这里谎报"已终止"。
        accepted, rejected, accepted_ids = await terminate_all_user_sessions(
            emby_user_id, sessions,
            reason=f"同时播放超出限制({stream_count}/{user_limit})"
        )
        violation_msg += f"\n✅ 已下发停止指令: {accepted} 个流"
        if rejected > 0:
            violation_msg += f" | ❌ 服务端拒绝: {rejected} 个流"
        if accepted > 0:
            violation_msg += (
                f"\n🔎 终止复验结果将在 {_STOP_VERIFY_FOLLOWUP} 秒后补发"
                f"（客户端断流通常有 20~30 秒延迟，此刻流可能还在播）"
            )

        # 更新警告计数
        new_warn_count = current_warns + 1
        sql_update_emby(Emby.tg == tg_id, concurrent_warn_count=new_warn_count)
        if tg_id:
            await _sync_kk_panels(tg_id)

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

        # 向用户发送警告（文案按实际下发结果生成，避免"没停掉却说已终止"）
        if tg_id:
            if accepted == 0:
                enforce_line = (
                    "⚠️ 播放流停止指令**未能下发**，请**立即手动停止播放**，"
                    "否则将直接封禁账号。"
                )
            elif rejected == 0:
                enforce_line = (
                    "已向你的播放设备下发停止指令（客户端通常需要 20~30 秒才会真正断开）。"
                )
            else:
                enforce_line = (
                    f"已下发 {accepted} 个流的停止指令，另有 {rejected} 个流服务端拒绝，"
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

        # 延迟复验：Stop 返回 204 只代表服务端接受了指令，客户端断流有 20~30 秒延迟。
        # 这里起一个后台任务，等 _STOP_VERIFY_FOLLOWUP 秒后重新拉会话列表，
        # 把**实测**的断开数量补发到群里 —— 不阻塞本轮检测，也不当场谎报"已终止"。
        if accepted_ids:
            _schedule_stop_verify(accepted_ids, user_name)

        LOGGER.info(
            f"同时播放限制: user={user_name}, streams={stream_count}, warns={new_warn_count}, "
            f"停止指令已下发={accepted}, 服务端拒绝={rejected}"
        )


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


async def _sync_kk_panels(uid=None):
    """
    把已打开的 /kk 面板同步到最新警告计数。

    并发检测改了 concurrent_warn_count 却不刷新面板时，面板就会停在改动前的
    值；管理员随后点「➖ 警告-1」，写回的值可能正好等于面板上已经显示的值，
    Telegram 会以 MESSAGE_NOT_MODIFIED 拒绝编辑，表现为「操作成功但数字没动」。

    纯尽力而为：任何异常都吞掉，绝不能让面板同步拖垮每 60 秒跑一次的检测任务。
    """
    try:
        from bot.modules.panel.kk import sync_kk_panel
        await sync_kk_panel(uid)
    except Exception as e:
        LOGGER.debug(f"同步 /kk 面板跳过: {type(e).__name__}: {e}")


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
            await _sync_kk_panels()
            return True
        except Exception as e:
            LOGGER.error(f"重置警告计数失败: {e}")
            session.rollback()
            return False
