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

# 会话列表端点必须带 ActiveWithinSeconds。
#
# 线上实测（51.89.6.109 / Emby 4.10.0.40）：裸 `GET /emby/Sessions` 返回
# 2,970,545 B / 4037 条 session，其中真正 NowPlayingItem 非空的只有 7~8 条 ——
# 4000 多条只在进程内存里、永不清理的僵尸会话。本模块每 concurrent_play_check_interval
# 秒拉一次，等于每 60 秒往 Emby 的 18GB 托管堆上砸一次 3MB 序列化分配，
# 是「Emby 假死」的固定放大器之一。
# 加 `?ActiveWithinSeconds=300` 后：89,940 B / 75 条 / 0.66s（体积 1/33）。
# 正在播放的会话每 10 秒上报一次进度，300 秒窗口不会漏。
# 本模块本来就只统计 `session.get("NowPlayingItem")` 的会话，所以语义完全等价。
try:  # 限流模块缺失/导入失败时退回默认窗口，绝不影响巡检本身
    from bot.func_helper.register_throttle import sessions_endpoint as _sessions_endpoint
except Exception:  # pragma: no cover
    def _sessions_endpoint() -> str:
        return "/emby/Sessions?ActiveWithinSeconds=300"

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

# 违规通知里「累计警告: N / 阈值」那个 N 的占位符。
# 原因见 check_concurrent_play_limit() 里更新警告计数处：计数必须在终止流的
# 秒级 I/O **之后**重新读一次再写，所以构建通知文案时还拿不到最终数字，
# 先用占位符占位，写完计数再回填，避免同一条通知里出现两个互相矛盾的次数。
_WARN_N_PLACEHOLDER = "\x00WARN_N\x00"

# ══════════════════════════════════════════════════════════════════════════
# 「临时封禁踢流」：这台 Emby 上唯一真正能停流的手段
#
# 为什么不能靠 POST /Sessions/{Id}/Playing/Stop：
#   那个接口本质是**通过会话的远程控制通道向客户端下发一条 playstate 指令**，
#   不是服务端杀流。2026-10-06 在 ChaPanda（Emby 4.10.0.40）实测：
#     · 3643 个会话里 SupportsRemoteControl=True 的 0 个；
#     · GET /Sessions?ControllableByUserId=<userId> 对**管理员自己**也返回 0 条
#       —— 服务器自己就认为这些会话不可控；
#     · 对正在播放的会话调 Stop，HTTP 204，但 +5/+15/+30s 复查仍在播放；
#       Playing/Pause 同样无效。整条远程控制通道对这批客户端是死的。
#   其余候选也都实测排除：
#     · DELETE /Sessions/{Id}  —— 4.10 根本没有这个路由（返回 404）
#     · DELETE /Videos/ActiveEncodings —— 需要 PlaySessionId，而会话 DTO 里
#       根本没有这个字段；且实测在播会话全是 DirectStream，无转码任务
#     · DELETE /Devices?Id= —— 只吊销 token，**已建立的流会继续下载**；
#       且带保存密码的客户端会自动重登，实测两次结果相反，不可靠
#     · SimultaneousStreamLimit —— 需要 Emby Premiere（本站未开通），
#       实测设成 1 后第二条流照样能播，服务端完全不执行
#
# 唯一有效的是把用户策略的 IsDisabled 置真：禁用后 Emby **服务器自己**
# 会把该用户的会话清成 0，且重新认证被拒（403 账户已被禁用）。实测停流
# 窗口 5~67 秒 —— 所以不能做 1 秒级短封禁，客户端要连续多次请求失败才放弃。
#
# 代价与风险：
#   · 会把该用户**当时所有**流一起踢掉（不只多出来的那条）
#   · 若在「禁用」与「还原」之间进程被杀，用户会被**永久锁死** —— 因此
#     到期时间必须落库（Emby.kick_until），启动时无条件扫出来还原
# ══════════════════════════════════════════════════════════════════════════

# 临时禁用保持多少秒再还原。
# 实测客户端放弃播放的窗口是 5~67 秒，取 45 秒落在窗口内且不至于太久。
_KICK_HOLD_SECONDS = 45

# 还原后的校验重试次数与间隔：写入成功 ≠ 真的生效，必须回读确认。
_KICK_VERIFY_ATTEMPTS = 3
_KICK_VERIFY_INTERVAL = 3

# 异步还原任务的强引用集合（同 _VERIFY_TASKS，避免被 GC 回收）。
_KICK_TASKS = set()

# 还原失败的群通报节流。
#
# 为什么需要：还原失败时会**刻意保留** kick_until 让后续重试（这是对的，否则
# 用户被锁死且无人知道）。但周期巡检每 `concurrent_play_check_interval` 秒就会
# 再捡起这条记录、再失败、再发一条 🚨 —— 一个策略永久读不到（或用户已被删）
# 的账号会让群里每 60 秒刷一条同样的告警，很快就被管理员无视，反而掩盖了真正
# 的新问题。
#
# 策略：同一个用户 + **同一个错误原因**在 _RESTORE_ALERT_INTERVAL 秒内只通报
# 一次；错误原因变了立刻通报（那是新信息）。日志不受节流影响，每次失败都记。
_RESTORE_ALERT_INTERVAL = 1800
_RESTORE_ALERTS = {}


def _restore_alert_key(tg_id, emby_user_id) -> str:
    """
    节流桶的 key。

    带上 embyid 是因为 `tg_id` 可能为 `None`（记录里没有关联的 TG 用户）：
    若只用 `str(tg_id)`，所有无 tg 的记录会挤进同一个 `"None"` 桶里互相抑制，
    一条失败会把另一条的告警吃掉。
    """
    return f"{tg_id}:{emby_user_id}"


def _should_alert_restore_failure(tg_id, emby_user_id, error) -> bool:
    """还原失败的群通报节流判定（口径见 _RESTORE_ALERTS 的说明）。"""
    now = _utcnow()
    key = _restore_alert_key(tg_id, emby_user_id)
    last = _RESTORE_ALERTS.get(key)
    if last is not None and last[0] == error:
        if (now - last[1]).total_seconds() < _RESTORE_ALERT_INTERVAL:
            return False
    _RESTORE_ALERTS[key] = (error, now)
    return True


def _clear_restore_alert(tg_id, emby_user_id):
    """
    还原**成功**后清掉该用户的节流记录。

    为什么必须清：节流的目的是压制「同一次事故被周期巡检反复重试」造成的刷屏，
    **不是**压制新事故。若不清，同一用户下一次事故的原因恰好相同、且落在 30 分钟
    窗口内，告警会被当成"重复"直接抑制 —— 群里一条 🚨 都没有，而用户是真的又被
    锁在门外了。这与本项目一路在修的"静默失败"是同一类问题。
    """
    _RESTORE_ALERTS.pop(_restore_alert_key(tg_id, emby_user_id), None)


def _now_str():
    """返回当前北京时间字符串"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


async def get_sessions_by_user():
    """
    获取所有活跃会话，按 Emby 用户ID 分组
    :return: {emby_user_id: [session, ...], ...}
    """
    result = await emby._request("GET", _sessions_endpoint())
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
        result = await emby._request("GET", _sessions_endpoint())
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


async def _verify_stop_followup(session_ids, user_name, wait: int = None, emby_user_id=None):
    """
    延迟复验：确认这些会话是否**真的**断流，并把实测结果补发到群里。

    单独补发而不是当场下结论的原因见模块顶部 _STOP_VERIFY_FOLLOWUP 的说明：
    Stop 返回 204 时客户端往往还在播，当场宣称"已终止"会与 Emby 活动页矛盾。

    :param emby_user_id: 有值时**按用户复验**（见下）；这是唯一能抓住"重连"的口径。

    【为什么按用户复验才是对的】按 session id 复验有个致命盲点：客户端一旦重连
    就拿到**新的 session id**，旧 id 从列表里消失 → `still` 为空 → 通报
    "已确认全部断开"。而真相是"他换了条新连接接着看"。这与本项目一路在修的
    "自报成功"是同一类错误。所以只要知道 emby_user_id，就改用它去数**这个用户
    当前有几个流在播**；拿不到 emby_user_id 时才退回 session id 口径，并且
    **措辞必须承认该口径的局限**，不能说得像确证过一样。
    """
    wait = _STOP_VERIFY_FOLLOWUP if wait is None else wait
    try:
        await asyncio.sleep(wait)
        total = len(session_ids)

        if emby_user_id:
            playing = await _user_sessions_playing(emby_user_id)
            if playing is None:
                await send_group_announcement(
                    f"🔎 **终止复验**\n用户: `{user_name}`\n"
                    f"⚠️ 无法获取会话状态，**未能确认**是否已停止，请到 Emby 活动页核对。"
                )
                LOGGER.warning(f"终止复验: user={user_name}, 结果=无法确认")
                return
            if playing == 0:
                await send_group_announcement(
                    f"🔎 **终止复验**\n用户: `{user_name}`\n"
                    f"✅ 该用户**当前没有任何播放流**（本轮已断开 {total} 个，未重连）。"
                )
            else:
                await send_group_announcement(
                    f"🔎 **终止复验**\n用户: `{user_name}`\n"
                    f"⚠️ 该用户**仍有 {playing} 个流在播放**（本轮下发停止指令 {total} 个）"
                    f"—— 停止指令未能生效，请手动处理。"
                )
            LOGGER.info(
                f"终止复验: user={user_name}, 按用户复验, 仍在播放={playing}, 等待={wait}s"
            )
            return

        # 退回按 session id 复验（不知道 emby_user_id）：口径有局限，措辞必须说清
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
                f"✅ 原会话已全部断开: **{stopped}/{total}** 个。\n"
                f"⚠️ 本次只能按会话 ID 复验，**若他在此期间重连则会漏判**，请到 Emby 活动页核对。"
            )
        else:
            await send_group_announcement(
                f"🔎 **终止复验**\n用户: `{user_name}`\n"
                f"⚠️ 原会话 {total} 个中仍有 **{len(still)}** 个在播放（{wait} 秒后）\n"
                f"请手动处理仍在播放的会话。"
            )
        LOGGER.info(
            f"终止复验: user={user_name}, 按会话 ID 复验, 已断开={stopped}, "
            f"仍在播放={len(still)}, 等待={wait}s"
        )
    except Exception as e:
        LOGGER.error(f"终止复验失败: user={user_name}, error={type(e).__name__}: {e}")


def _schedule_stop_verify(session_ids, user_name, wait: int = None, emby_user_id=None):
    """启动异步复验任务，并持有强引用避免被 GC 回收。"""
    task = asyncio.create_task(
        _verify_stop_followup(session_ids, user_name, wait, emby_user_id)
    )
    _VERIFY_TASKS.add(task)
    task.add_done_callback(_VERIFY_TASKS.discard)
    return task


async def _user_sessions_playing(emby_user_id: str):
    """
    重新拉会话列表，返回该用户**当前**仍在播放的会话数。

    与 `_sessions_still_playing()` 的关键区别：这里按 **UserId** 过滤，
    而不是按之前抓到的 session id。

    为什么必须这样：解封后用户重连会生成**新的 session id**。按旧 id 查，
    旧 id 永远不在列表里，于是**无论用户有没有重连都会报"已断开"** ——
    那是假证据，恰好把"踢流没成功、用户又连回来了"这个真实失败掩盖掉。

    :return: int；**取不到会话列表时返回 None**（无法确认），调用方不得当成 0
    """
    try:
        result = await emby._request("GET", _sessions_endpoint())
    except Exception as e:
        LOGGER.warning(f"复验会话状态失败（按无法确认处理）: {type(e).__name__}: {e}")
        return None
    if not result.success or not isinstance(result.data, list):
        LOGGER.warning(
            f"复验会话状态失败（按无法确认处理）: {getattr(result, 'error', None) or '返回不是列表'}"
        )
        return None

    return sum(
        1 for s in result.data
        if s.get("UserId") == emby_user_id and s.get("NowPlayingItem")
    )


async def _verify_kick_followup(emby_user_id: str, user_name: str, wait: int, stream_count: int):
    """
    「临时封禁踢流」的延迟复验：解封之后**按 UserId** 重新统计该用户还有几个流。

    调用时机必须晚于还原时刻，否则是在"账号还禁着"的时候复查，看到的"0 个流"
    根本不能说明解封之后用户不会重连。
    """
    try:
        await asyncio.sleep(wait)

        # 【第一步：账号到底解封了没有】这是**安全事实**，也是本次复验的头条。
        #
        # 为什么必须放在查流数**之前**（顺序很关键，有两个独立理由）：
        #   1) 不查状态就报"账号已解封"是彻头彻尾的假话 —— 他 0 个流**不是**因为他
        #      没重连，而是因为他根本连不上。`_restore_kick` 还原失败时会**刻意保留**
        #      kick_until，那时用户还锁着。群里会同时出现「🚨 还原失败…该用户当前仍
        #      处于禁用状态」和「✅ 账号已解封…未重连」两条互相矛盾的消息，后者是假的。
        #   2) 账号状态与 `/emby/Sessions` 是**两个互相独立的接口**。若先查会话而会话
        #      接口恰好挂了，就会直接 return「未能确认是否已停止」—— 把一个"用户被锁在
        #      门外"的安全事故掩盖成一句含糊话。先查状态就不会被会话接口的故障连累。
        disabled = await emby.is_user_disabled(emby_user_id)
        if disabled is None:
            await send_group_announcement(
                f"🔎 **踢流复验**\n用户: `{user_name}`\n"
                f"⚠️ 无法读取账号状态，**未能确认**是否已解封，请到 Emby 后台核对。"
            )
            LOGGER.warning(f"踢流复验: user={user_name}, 结果=无法确认账号状态")
            return
        if disabled:
            await send_group_announcement(
                f"🔎 **踢流复验**\n用户: `{user_name}`\n"
                f"🚨 账号**仍处于禁用状态**（到期还原失败），该用户现在看不了片，"
                f"请管理员手动解封。"
            )
            LOGGER.error(f"踢流复验: user={user_name}, 账号仍被禁用（还原失败）")
            return

        # 【第二步】账号确实已解封了，现在谈流才有意义
        playing = await _user_sessions_playing(emby_user_id)
        if playing is None:
            await send_group_announcement(
                f"🔎 **踢流复验**\n用户: `{user_name}`\n"
                f"✅ 账号已解封；但⚠️ 无法获取会话状态，**未能确认**是否已停止，"
                f"请到 Emby 活动页核对。"
            )
            LOGGER.warning(f"踢流复验: user={user_name}, 结果=已解封但无法确认流状态")
            return

        if playing == 0:
            await send_group_announcement(
                f"🔎 **踢流复验**\n用户: `{user_name}`\n"
                f"✅ 账号已解封，该用户**当前没有任何播放流**"
                f"（原有 {stream_count} 个已断开，未重连）。"
            )
        else:
            await send_group_announcement(
                f"🔎 **踢流复验**\n用户: `{user_name}`\n"
                f"⚠️ 账号已解封，但该用户**仍有 {playing} 个流在播放**"
                f"（原有 {stream_count} 个）—— 很可能是解封后重连，请手动处理。"
            )
        LOGGER.info(
            f"踢流复验: user={user_name}, 解封后仍在播放={playing}, "
            f"原有={stream_count}, 等待={wait}s"
        )
    except Exception as e:
        LOGGER.error(f"踢流复验失败: user={user_name}, error={type(e).__name__}: {e}")


def _schedule_kick_verify(emby_user_id: str, user_name: str, wait: int, stream_count: int):
    """启动踢流复验任务（按 UserId 判定），并持有强引用避免被 GC 回收。"""
    task = asyncio.create_task(
        _verify_kick_followup(emby_user_id, user_name, wait, stream_count)
    )
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
            # 通知放在停止指令被接受之后：避免"弹窗说已终止、实际没下发"。
            # 文案必须诚实：Stop 只是**下发了一条指令**，本服务器上这批客户端
            # 根本不支持远程控制（见文件上方 _KICK_HOLD_SECONDS 处的实测说明），
            # 所以这里不能说"已被终止"。
            await _notify_session(session_id, f"🚫 {reason}，服务端已要求停止播放。")
            continue

        LOGGER.warning(f"终止会话失败: session={session_id}, user={emby_user_id}, error={stop_result.error}")

        rejected += 1

    return accepted, rejected, accepted_ids


def _utcnow():
    """当前 UTC naive 时间。

    统一口径：`Emby.kick_until` 列存的就是 UTC naive，比较时也必须用同一个口径，
    否则在非 UTC 时区的容器里会算错几小时，表现为「刚禁用就立刻还原」或
    「迟迟不还原」。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def kick_user_streams(emby_user_id: str, tg_id: int = None,
                            hold_seconds: int = _KICK_HOLD_SECONDS) -> dict:
    """
    真正终止用户的播放流：**临时禁用 Emby 账号**，到期自动还原。

    这是本服务器上唯一有效的停流手段（原因见文件上方 _KICK_HOLD_SECONDS 的说明）。
    调用后立即返回，还原由后台任务完成；真实断流结果由复验任务补报。

    :param emby_user_id: Emby 用户ID
    :param tg_id: Telegram 用户ID（**必须提供**，落库要用；没有就无法安全地临时禁用）
    :param hold_seconds: 保持禁用的秒数
    :return: dict(disabled, hold_seconds, until, error)
    """
    if tg_id is None:
        # 没有 tg_id 就落不了库。绝不能在这种情况下禁用账号：进程一旦在
        # 禁用与还原之间死掉，就再也没人知道该给谁解封。
        LOGGER.error(f"缺少 tg_id，拒绝临时禁用（无法落库，有永久锁死风险）: emby_id={emby_user_id}")
        return {"disabled": False, "hold_seconds": hold_seconds, "until": None,
                "error": "missing_tg_id"}

    until = _utcnow() + timedelta(seconds=hold_seconds)

    # 【护栏一】先读当前禁用状态，只有"本来是启用的"才允许做临时封禁。
    #
    # 反例（必须拦住）：管理员刚手动封了某人（IsDisabled=true），但他的客户端
    # 还要 5~67 秒才断流，此时检测任务仍能看到他的会话。若不加这道判断，就会
    # 把"别人的封禁"当成"我们自己的临时封禁"，45 秒后的还原会**把管理员的封禁
    # 悄悄解开** —— 管理员会看到"刚封的人自己解封了"。
    # 同理，若上一次踢流还没还原（理论上不该发生，因为禁用后就没有会话了），
    # 也不能重复计时。
    current_disabled = await emby.is_user_disabled(emby_user_id)
    if current_disabled is None:
        # 读不到状态就不动手：还原时不知道该还原成什么，宁可不踢
        LOGGER.error(f"无法读取用户禁用状态，拒绝临时禁用: emby_id={emby_user_id}")
        return {"disabled": False, "hold_seconds": hold_seconds, "until": None,
                "error": "state_unknown"}
    if current_disabled:
        LOGGER.info(f"用户已处于禁用状态（非本次踢流所为），跳过临时禁用: emby_id={emby_user_id}")
        return {"disabled": False, "hold_seconds": hold_seconds, "until": None,
                "error": "already_disabled"}

    # 【顺序不能反】先落库，再禁用。
    # 反过来的话，「禁用成功、落库失败」就永久锁死了用户；而现在这种顺序下，
    # 最坏情况只是留下一条 kick_until 却从没禁用过 —— 还原任务把 IsDisabled
    # 写成 False 是无副作用的空操作。
    if not sql_update_emby(Emby.tg == tg_id, kick_until=until):
        LOGGER.error(f"写入 kick_until 失败，拒绝临时禁用（避免永久锁死）: tg={tg_id}")
        return {"disabled": False, "hold_seconds": hold_seconds, "until": None,
                "error": "persist_failed"}

    disabled = await emby.set_user_disabled(emby_user_id, True)
    if not disabled:
        # 禁用没成功，清掉待还原标记，免得后台任务去做无意义的"还原"
        sql_update_emby(Emby.tg == tg_id, kick_until=None)
        LOGGER.error(f"临时禁用失败: emby_id={emby_user_id}, tg={tg_id}")
        return {"disabled": False, "hold_seconds": hold_seconds, "until": None,
                "error": "disable_failed"}

    LOGGER.info(f"已临时禁用（踢流）: emby_id={emby_user_id}, tg={tg_id}, 保持 {hold_seconds}s")
    task = asyncio.create_task(_kick_restore_worker(emby_user_id, tg_id, hold_seconds))
    _KICK_TASKS.add(task)
    task.add_done_callback(_KICK_TASKS.discard)
    return {"disabled": True, "hold_seconds": hold_seconds, "until": until, "error": None}


async def _kick_restore_worker(emby_user_id: str, tg_id: int, hold_seconds: int):
    """等待 hold_seconds 后还原临时封禁。被取消时也必须还原。"""
    try:
        await asyncio.sleep(hold_seconds)
    except asyncio.CancelledError:
        # 进程正常退出会取消任务；此时若不还原，用户就被锁死了。
        await _restore_kick(emby_user_id, tg_id, "任务被取消")
        raise
    await _restore_kick(emby_user_id, tg_id, "到期")


async def _restore_kick(emby_user_id: str, tg_id: int, source: str) -> bool:
    """
    还原临时封禁，并**回读校验**。

    关键点：只有回读确认 `IsDisabled=False` 才清 `kick_until`。
    写入成功 ≠ 真的生效；校验不过就**故意保留** kick_until，让启动自恢复和
    周期巡检继续重试，同时把问题喊到群里 —— 此刻用户还锁着，必须让人知道。
    """
    last_error = None

    # 【必须先确认这次禁用还归我们管】
    # `kick_until` 被清空 = 这个禁用状态已被"接管"：管理员封禁了该用户
    # （ban_user 会清标记），或已经还原过一次。此时若还去写 IsDisabled=False，
    # 就会把**真正的封禁**悄悄解开，管理员会看到"刚封的人自己解封了"。
    if tg_id is not None:
        row = sql_get_emby(tg=tg_id)
        if row is None or getattr(row, "kick_until", None) is None:
            LOGGER.info(f"kick_until 已清空，跳过还原（禁用状态已被接管）: tg={tg_id}")
            return True

    for attempt in range(1, _KICK_VERIFY_ATTEMPTS + 1):
        ok = await emby.set_user_disabled(emby_user_id, False)
        if ok:
            state = await emby.is_user_disabled(emby_user_id)
            if state is False:
                # Emby 侧已确认解封 —— 用户已经能看了，这是关键事实。
                # 清标记失败不影响用户，但**必须如实记日志**：残留的 kick_until
                # 会让后续巡检再空跑一次（写 IsDisabled=False 是幂等的），
                # 而不是像以前那样无论清没清成功都打印"已还原并校验通过"。
                cleared = True
                if tg_id is not None:
                    cleared = sql_update_emby(Emby.tg == tg_id, kick_until=None)
                # 这次事故结束了 —— 清掉节流记录，让**下一次**事故无论原因是否
                # 相同都能正常告警（详见 _clear_restore_alert 的说明）。
                _clear_restore_alert(tg_id, emby_user_id)
                if cleared:
                    LOGGER.info(f"临时封禁已还原并校验通过（{source}）: emby_id={emby_user_id}, tg={tg_id}")
                else:
                    LOGGER.warning(
                        f"临时封禁已还原并校验通过，但 kick_until 清除失败（{source}）: "
                        f"emby_id={emby_user_id}, tg={tg_id}。用户已解封，"
                        f"残留标记只会让后续巡检多空跑一次"
                    )
                return True
            last_error = f"回读仍是 IsDisabled={state}"
        else:
            last_error = "写入 IsDisabled=False 失败"

        if attempt < _KICK_VERIFY_ATTEMPTS:
            await asyncio.sleep(_KICK_VERIFY_INTERVAL)

    LOGGER.error(
        f"临时封禁还原失败（{source}）: emby_id={emby_user_id}, tg={tg_id}, "
        f"尝试 {_KICK_VERIFY_ATTEMPTS} 次, 最后错误={last_error}。"
        f"kick_until 保留，将由启动自恢复/周期巡检重试"
    )
    # 日志每次都记（上面那行），但群通报要节流：周期巡检会不断重试失败记录，
    # 不节流的话一个永久失败的账号会让群里每 60 秒刷一条同样的 🚨。
    if _should_alert_restore_failure(tg_id, emby_user_id, last_error):
        await send_group_announcement(
            f"🚨 **临时封禁还原失败**\n"
            f"用户 TG: `{tg_id}`\n"
            f"Emby ID: `{emby_user_id}`\n"
            f"错误: `{last_error}`\n"
            f"⚠️ 该用户当前仍处于禁用状态，机器人会自动重试；若持续失败请手动解封。\n"
            f"（同一原因 {_RESTORE_ALERT_INTERVAL // 60} 分钟内不重复通报，日志不受限）"
        )
    return False


async def restore_pending_kicks(only_expired: bool = False) -> int:
    """
    还原「待还原」的临时封禁（崩溃自恢复 + 周期巡检）。

    :param only_expired: True 只还原已到期的（周期巡检用，避免打断进行中的踢流）；
                         False 无条件全部还原（**启动时用这个** —— 宁可少踢一次流，
                         也绝不能因为上次进程异常退出就把用户永久锁死）
    :return: 成功还原的条数
    """
    from bot.sql_helper.sql_emby import sql_get_pending_kicks

    rows = sql_get_pending_kicks()
    if not rows:
        return 0

    now = _utcnow()
    restored = 0
    for tg, embyid, until in rows:
        if only_expired and until is not None and until > now:
            continue  # 还在保持期内，交给 _kick_restore_worker
        if not embyid:
            # 没有 embyid 无从还原，清掉标记避免每轮都报错
            LOGGER.error(f"待还原记录缺少 embyid，只能清除标记: tg={tg}")
            if tg is not None:
                sql_update_emby(Emby.tg == tg, kick_until=None)
            continue
        if await _restore_kick(embyid, tg, "启动自恢复" if not only_expired else "周期巡检"):
            restored += 1
    return restored


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
            # 清掉待还原标记，声明"现在的禁用是终态，别动它"。
            #
            # 注：`emby_change_policy()` 成功后**已经**在收口点清了 `kick_until`
            # （见该方法里的「接管声明」注释，那里覆盖了全仓库 15+ 处封禁路径）。
            # 这里再清一次是刻意的双保险：万一将来有人把 ban_user 改成走别的
            # 禁用机制（例如直接 POST Policy），这行仍能挡住"刚封的人被还原任务
            # 悄悄解开"。代价只是一次多余的 UPDATE。
            if tg_id is not None:
                sql_update_emby(Emby.tg == tg_id, kick_until=None)
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

    # 安全网：还原「已到期但后台还原任务没跑成」的临时封禁。
    # 正常路径由 _kick_restore_worker 负责；这里只兜底两类意外：
    #   ① 还原任务写入/校验失败后保留了 kick_until（见 _restore_kick）
    #   ② 事件循环被重启过、任务丢了，但进程没重启（所以启动自恢复没跑到）
    # only_expired=True 保证不会打断正在进行中的踢流（那种 kick_until 还在未来）。
    try:
        await restore_pending_kicks(only_expired=True)
    except Exception as e:
        LOGGER.error(f"周期巡检临时封禁失败: {type(e).__name__}: {e}")

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
        # 刻意**不在这里**读 concurrent_warn_count：此刻距离真正写计数还隔着
        # terminate_all_user_sessions() 这段秒级 I/O，读到的值可能已经过期
        # （管理员在此期间重置/减过警告）。计数一律在写入前重新读取，见下方
        # 「更新警告计数」处。e 里其余字段（name/lv/tg）不受影响，可继续用。

        # 构建违规信息
        now_str = _now_str()
        violation_msg = (
            f"⚠️ **同时播放限制警告**\n\n"
            f"用户: `{user_name}` (TG: `{tg_id}`)\n"
            f"Emby ID: `{emby_user_id}`\n"
            f"当前播放流: **{stream_count}** 个 (限制: **{user_limit}** 个)\n"
            f"检测时间: {now_str}\n"
            f"累计警告: **{_WARN_N_PLACEHOLDER}** / **{warn_threshold}** 次\n"
        )

        # 列出正在播放的内容
        for idx, session in enumerate(sessions, 1):
            now_playing = session.get("NowPlayingItem", {})
            media_name = now_playing.get("Name", "未知")
            client_name = session.get("Client", "未知设备")
            violation_msg += f"  {idx}. 🎬 `{media_name}` | 📱 {client_name}\n"

        # ── 第一步：礼貌性地尝试下发 Stop 指令 ──
        #
        # 这里拿到的是"服务端是否**接受**了停止指令"，不是"客户端是否已断流"。
        # 本服务器的客户端群体**实测完全不支持远程控制**（证据见文件上方
        # _KICK_HOLD_SECONDS 处的说明：SupportsRemoteControl 0/3643、
        # ControllableByUserId 对管理员自己也返回空、Stop 返回 204 但 30 秒后仍在播）。
        # 保留这一步是因为它对将来某个真正支持远程控制的客户端仍然有效、且无副作用，
        # 但**绝不能**把它当成已经停流 —— 真正停流靠下面的临时封禁。
        accepted, rejected, accepted_ids = await terminate_all_user_sessions(
            emby_user_id, sessions,
            reason=f"同时播放超出限制({stream_count}/{user_limit})"
        )
        violation_msg += f"\n📤 已下发停止指令: {accepted} 个流（本服务器客户端多不支持远程控制，通常无效）"
        if rejected > 0:
            violation_msg += f" | ❌ 服务端拒绝: {rejected} 个流"

        # 更新警告计数。
        #
        # 【必须在**这里**重新读一次，不能沿用循环开头读到的 current_warns】
        # 上面 terminate_all_user_sessions() 是秒级的 Emby I/O（客户端真正断流
        # 还要 20~30 秒），这中间管理员完全可能在 /kk 面板上点「🔄 重置警告」或
        # 「➖ 警告-1」。而这里是**绝对值写入**，沿用旧值会把管理员的调整静默抹掉：
        #     任务读到 5 → 管理员重置为 0（面板提示"已重置为 0"）→ 任务回来写 6
        # 重置就此消失；更糟的是 6 若 ≥ 阈值，同一个流程紧接着就 ban_user ——
        # 管理员刚重置完，用户反而立刻被自动封禁。
        #
        # 重新读与写之间**没有 await**：asyncio 是协作式调度，不会在此处切走，
        # 所以「读最新值 → 写 +1」这一段对事件循环是原子的，不会再有丢失更新。
        # （管理员侧 kk.py 的读与写之间同样没有 await，两边因此互为原子操作。）
        #
        # 注意不能改成数据库端原子自增（Emby.concurrent_warn_count + 1）：那样
        # 拿不到写入后的值，而下面的阈值判断与通知文案都需要它。
        fresh = sql_get_emby(tg=tg_id)
        current_warns = int(getattr(fresh, "concurrent_warn_count", None) or 0)
        new_warn_count = current_warns + 1
        sql_update_emby(Emby.tg == tg_id, concurrent_warn_count=new_warn_count)
        # 把通知文案里的占位符换成最终数字，保证全文只出现一个「累计警告」值
        violation_msg = violation_msg.replace(_WARN_N_PLACEHOLDER, str(new_warn_count))
        if tg_id:
            await _sync_kk_panels(tg_id)

        # ── 第二步：真正停流 ──
        #
        # 这台服务器上唯一由服务端自己执行、真正能停流的杠杆，就是把用户策略的
        # `IsDisabled` 置真。两条互斥的路：
        #   · 已达阈值 → ban_user（禁用是终态，它会顺手清掉 kick_until）
        #   · 未达阈值 → kick_user_streams（临时禁用几十秒后自动还原）
        # 之所以互斥：同时做的话，临时踢流的还原任务会把刚下的封禁解开。
        banned = False
        kicked = False
        kick = {}
        if new_warn_count >= warn_threshold:
            # 封禁用户
            banned = await ban_user(emby_user_id, tg_id)
            if banned:
                violation_msg += f"\n\n🔴 **警告次数已超限 ({new_warn_count}/{warn_threshold})，账号已被自动封禁！**"
            else:
                violation_msg += f"\n\n🔴 **警告次数已超限 ({new_warn_count}/{warn_threshold})，封禁失败，请手动处理！**"
        else:
            kick = await kick_user_streams(emby_user_id, tg_id)
            kicked = bool(kick.get("disabled"))
            if kicked:
                violation_msg += (
                    f"\n\n🦶 **已临时禁用该账号 {kick['hold_seconds']} 秒以强制断流**"
                    f"（该客户端不支持远程停止指令，只能这样真正踢掉流），到期自动解封。"
                )
            else:
                violation_msg += (
                    f"\n\n⚠️ **强制断流失败**（{kick.get('error')}），"
                    f"该用户当前可能仍在超限播放，请手动处理。"
                )
            violation_msg += f"\n⚠️ 再犯 **{warn_threshold - new_warn_count}** 次将自动封禁账号！"

        # 向用户发送警告。文案按**实际做了什么**生成：
        # 绝不说"已终止"—— 本服务器的 Stop 是空操作，只有禁用账号才真正断流。
        if tg_id:
            if banned:
                enforce_line = "你的账号已被封禁。"
            elif kicked:
                enforce_line = (
                    f"已**临时禁用你的账号 {kick['hold_seconds']} 秒**以强制断开播放"
                    f"（你的播放器不支持远程停止指令），到期会自动解封，无需联系管理员。"
                )
            else:
                enforce_line = (
                    "⚠️ **强制断流未能生效**，请**立即手动停止播放**，"
                    "否则将直接封禁账号。"
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

        # 延迟复验：把**实测**的断开数量补发到群里，绝不在这里当场宣称"已终止"。
        #
        # 两条路径都**优先按 UserId** 复验（不是按旧 session id）：客户端重连会生成
        # 新 session id，按旧 id 查必然查不到，于是无论用户有没有重连都报"已断开"
        # —— 那是假证据。只有拿不到 emby_user_id 时才退回按 session id 的口径，
        # 那时通报里会明确写出"若重连则会漏判"。
        #
        # 等待时间也必须**晚于还原时刻**，否则是在"还禁着"的时候复查。
        if kicked:
            _schedule_kick_verify(
                emby_user_id, user_name, kick["hold_seconds"] + 15, stream_count
            )
        elif accepted_ids:
            _schedule_stop_verify(
                accepted_ids, user_name, emby_user_id=emby_user_id
            )

        LOGGER.info(
            f"同时播放限制: user={user_name}, streams={stream_count}, warns={new_warn_count}, "
            f"停止指令已下发={accepted}, 服务端拒绝={rejected}, "
            f"临时禁用踢流={kicked}, 封禁={banned}"
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
