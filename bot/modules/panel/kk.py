"""
kk - 纯装x
赠与账户，禁用，删除
"""
import pyrogram
from asyncio import sleep
from pyrogram import filters
from pyrogram.errors import BadRequest, FloodWait
from bot import bot, prefixes, owner, admins, LOGGER, extra_emby_libs, config
from bot.func_helper.emby import emby
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.fix_bottons import cr_kk_ikb, gog_rester_ikb
from bot.func_helper.msg_utils import deleteMessage, sendMessage, editMessage, escape_markdown
from bot.func_helper.utils import judge_admins, cr_link_two, tem_deluser
from bot.sql_helper.sql_emby import sql_add_emby, sql_get_emby, sql_update_emby, Emby


# ──────────────────────────────────────────────────────────────────────
# /kk 面板的发送与刷新
#
# 每个「管理员 + 被查看用户」只保留最新一条面板：以前每次 /kk 都新发一条
# 且从不删旧的，管理员很容易对着一条陈旧面板操作。那条面板上的计数是它
# 上一次渲染时的值，点完「➖ 警告-1」重渲染出来的文本可能与它一字不差，
# Telegram 就会以 MESSAGE_NOT_MODIFIED 拒绝编辑 —— 现象正是「提示说改成了，
# 但面板上的数字没动」。
# ──────────────────────────────────────────────────────────────────────
_kk_panel_ids = {}


# 回调应答有时效，等太久没有意义：超过这个秒数就直接放弃重试。
# 不加这个上限的话，edit 与 send 各重试 3 次、每次 FloodWait 60s，
# 最坏会在回调协程里阻塞 400 秒以上。
_FLOODWAIT_MAX_WAIT = 30

# 面板刷新的三种结果。必须区分开，调用方要据此决定给管理员看什么提示：
#   EDITED    真的改了
#   UNCHANGED 面板本来就显示这个值（管理员会觉得「数字没动」，需要解释）
#   FAILED    真的失败了（必须告警，不能让管理员以为成功了）
_REFRESH_EDITED = "edited"
_REFRESH_UNCHANGED = "unchanged"
_REFRESH_FAILED = "failed"


async def _kk_send_with_floodwait(chat_id, text, keyboard, attempts=2):
    """发面板，FloodWait 自动重试（有上限）。失败返回 None。"""
    for i in range(attempts):
        try:
            return await bot.send_message(chat_id=chat_id, text=text,
                                          disable_web_page_preview=True, reply_markup=keyboard)
        except FloodWait as f:
            if f.value > _FLOODWAIT_MAX_WAIT or i == attempts - 1:
                LOGGER.warning(f"发送 /kk 面板被限流 {f.value}s，放弃重试 chat={chat_id}")
                return None
            LOGGER.warning(f"发送 /kk 面板被限流，等待 {f.value}s 后重试"
                           f"（第 {i + 1}/{attempts} 次）chat={chat_id}")
            await sleep(f.value * 1.2)
        except Exception as e:
            LOGGER.error(f"发送 /kk 面板失败 chat={chat_id}: {type(e).__name__}: {e}")
            return None
    return None


async def _send_kk_panel(chat_id, uid, text, keyboard):
    """
    发新面板，**成功之后**才删掉同一用户的旧面板。

    顺序很关键：先删后发的话，一旦发送失败（限流耗尽 / 没权限），管理员面前
    一条面板都不剩，比「停在旧值」更难排查。先发后删最坏只是多一条消息。
    """
    key = (chat_id, uid)
    sent = await _kk_send_with_floodwait(chat_id, text, keyboard)
    if sent is None:
        return None
    old = _kk_panel_ids.get(key)
    if old is not None and old != sent.id:
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=old)
        except Exception as e:
            LOGGER.debug(f"删除旧 /kk 面板失败 chat={chat_id} msg={old}: {e}")
    _kk_panel_ids[key] = sent.id
    return sent


# 管理用户
@bot.on_message(filters.command('kk', prefixes) & admins_on_filter)
async def user_info(_, msg):
    await deleteMessage(msg)
    if msg.reply_to_message is None:
        try:
            uid = int(msg.command[1])
            if not msg.sender_chat:
                if msg.from_user.id != owner and uid == owner:
                    return await sendMessage(msg,
                                             f"⭕ [{escape_markdown(msg.from_user.first_name)}](tg://user?id={msg.from_user.id})！不可以偷窥主人",
                                             timer=60)
            else:
                pass
            first = await bot.get_chat(uid)
        except (IndexError, KeyError, ValueError):
            return await sendMessage(msg, '**请先给我一个tg_id！**\n\n用法：/kk [tg_id]\n或者对某人回复kk', timer=60)
        except BadRequest:
            return await sendMessage(msg, f'{msg.command[1]} - 🎂抱歉，此id未登记bot，或者id错误', timer=60)
        except AttributeError:
            pass
        else:
            sql_add_emby(uid)
            text, keyboard = await cr_kk_ikb(uid, first.first_name)
            await _send_kk_panel(msg.chat.id, uid, text, keyboard)  # protect_content=True 移除禁止复制

    else:
        uid = msg.reply_to_message.from_user.id
        try:
            if msg.from_user.id != owner and uid == owner:
                return await msg.reply(
                    f"⭕ [{msg.from_user.first_name}](tg://user?id={msg.from_user.id})！不可以偷窥主人")
        except AttributeError:
            pass

        sql_add_emby(uid)
        text, keyboard = await cr_kk_ikb(uid, msg.reply_to_message.from_user.first_name)
        await _send_kk_panel(msg.chat.id, uid, text, keyboard)


# 封禁或者解除
@bot.on_callback_query(filters.regex('^user_ban-'))
async def kk_user_ban(_, call):
    if not judge_admins(call.from_user.id):
        return await call.answer("请不要以下犯上 ok？", show_alert=True)

    await call.answer("✅ ok")
    b = int(call.data.split("-")[1])
    if b in admins and b != call.from_user.id:
        return await editMessage(call,
                                 f"⚠️ 打咩，no，机器人不可以对bot管理员出手喔，请[自己](tg://user?id={call.from_user.id})解决",
                                 timer=60)

    first = await bot.get_chat(b)
    e = sql_get_emby(tg=b)
    if e.embyid is None:
        await editMessage(call, f'💢 ta 没有注册账户。', timer=60)
    else:
        text = f'🎯 管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id}) 对 [{first.first_name}](tg://user?id={b}) - {e.name} 的'
        if e.lv != "c":
            if await emby.emby_change_policy(emby_id=e.embyid, disable=True) is True:
                if sql_update_emby(Emby.tg == b, lv='c') is True:
                    text += f'封禁完成，此状态可在下次续期时刷新'
                    LOGGER.info(text)
                else:
                    text += '封禁失败，已执行，但数据库写入错误'
                    LOGGER.error(text)
            else:
                text += f'封禁失败，请检查emby服务器。响应错误'
                LOGGER.error(text)
        elif e.lv == "c":
            if await emby.emby_change_policy(emby_id=e.embyid):
                if sql_update_emby(Emby.tg == b, lv='b'):
                    text += '解禁完成'
                    LOGGER.info(text)
                else:
                    text += '解禁失败，服务器已执行，数据库写入错误'
                    LOGGER.error(text)
            else:
                text += '解封失败，请检查emby服务器。响应错误'
                LOGGER.error(text)
        await editMessage(call, text)
        await bot.send_message(b, text)


# 开通额外媒体库
@bot.on_callback_query(filters.regex('^embyextralib_unblock-'))
async def user_embyextralib_unblock(_, call):
    if not judge_admins(call.from_user.id):
        return await call.answer("请不要以下犯上 ok？", show_alert=True)
    await call.answer('🎬 正在为TA开启显示ing')
    tgid = int(call.data.split("-")[1])
    e = sql_get_emby(tg=tgid)
    if e.embyid is None:
        await editMessage(call, f'💢 ta 没有注册账户。', timer=60)
        return
    embyid = e.embyid
    success, rep = await emby.user(emby_id=embyid)
    if success:
        try:
            # 使用封装的显示额外媒体库方法
            re = await emby.show_folders_by_names(embyid, extra_emby_libs)
            
            if re is True:
                await editMessage(call, f'🌟 好的，管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id})\n'
                                        f'已开启了 [TA](tg://user?id={tgid}) 的额外媒体库权限\n{extra_emby_libs}')
            else:
                await editMessage(call,
                                  f'🌧️ Error！管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id})\n操作失败请检查设置！')
        except Exception as e:
            LOGGER.error(f"开启额外媒体库失败: {str(e)}")
            await editMessage(call,
                              f'🌧️ Error！管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id})\n操作失败请检查设置！')


# 隐藏额外媒体库
@bot.on_callback_query(filters.regex('^embyextralib_block-'))
async def user_embyextralib_block(_, call):
    if not judge_admins(call.from_user.id):
        return await call.answer("请不要以下犯上 ok？", show_alert=True)
    await call.answer('🎬 正在为TA关闭显示ing')
    tgid = int(call.data.split("-")[1])
    e = sql_get_emby(tg=tgid)
    if e.embyid is None:
        await editMessage(call, f'💢 ta 没有注册账户。', timer=60)
        return
    embyid = e.embyid
    success, rep = await emby.user(emby_id=embyid)
    if success:
        try:
            # 使用封装的隐藏额外媒体库方法
            re = await emby.hide_folders_by_names(embyid, extra_emby_libs)
            
            if re is True:
                await editMessage(call, f'🌟 好的，管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id})\n'
                                        f'已关闭了 [TA](tg://user?id={tgid}) 的额外媒体库权限\n{extra_emby_libs}')
            else:
                await editMessage(call,
                                  f'🌧️ Error！管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id})\n操作失败请检查设置！')
        except Exception as e:
            LOGGER.error(f"关闭额外媒体库失败: {str(e)}")
            await editMessage(call,
                              f'🌧️ Error！管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id})\n操作失败请检查设置！')


# 赠送资格
@bot.on_callback_query(filters.regex('^gift-'))
async def gift(_, call):
    if not judge_admins(call.from_user.id):
        return await call.answer("请不要以下犯上 ok？", show_alert=True)

    await call.answer("✅ ok")
    b = int(call.data.split("-")[1])
    if b in admins and b != call.from_user.id:
        return await editMessage(call,
                                 f"⚠️ 打咩，no，机器人不可以对bot管理员出手喔，请[自己](tg://user?id={call.from_user.id})解决")

    first = await bot.get_chat(b)
    e = sql_get_emby(tg=b)
    if e.embyid is None:
        link = await cr_link_two(tg=call.from_user.id, for_tg=b, days=config.kk_gift_days)
        await editMessage(call, f"🌟 好的，管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id})\n"
                                f'已为 [{first.first_name}](tg://user?id={b}) 赠予资格。前往bot进行下一步操作：',
                          buttons=gog_rester_ikb(link))
        LOGGER.info(f"【admin】：{call.from_user.id} 已发送 注册资格 {first.first_name} - {b} ")
    else:
        await editMessage(call, f'💢 [ta](tg://user?id={b}) 已注册账户。')


# 删除账户
@bot.on_callback_query(filters.regex('^closeemby-'))
async def close_emby(_, call):
    if not judge_admins(call.from_user.id):
        return await call.answer("请不要以下犯上 ok？", show_alert=True)

    await call.answer("✅ ok")
    b = int(call.data.split("-")[1])
    if b in admins and b != call.from_user.id:
        return await editMessage(call,
                                 f"⚠️ 打咩，no，机器人不可以对bot管理员出手喔，请[自己](tg://user?id={call.from_user.id})解决",
                                 timer=60)

    first = await bot.get_chat(b)
    e = sql_get_emby(tg=b)
    if e.embyid is None:
        return await editMessage(call, f'💢 ta 还没有注册账户。', timer=60)

    if await emby.emby_del(emby_id=e.embyid):
        sql_update_emby(Emby.embyid == e.embyid, embyid=None, name=None, pwd=None, pwd2=None, lv='d', cr=None, ex=None)
        tem_deluser()
        await editMessage(call,
                          f'🎯 done，管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id})\n等级：{e.lv} - [{first.first_name}](tg://user?id={b}) '
                          f'账户 {e.name} 已完成删除。')
        await bot.send_message(b,
                               f"🎯 管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id}) 已删除 您 的账户 {e.name}")
        LOGGER.info(f"【admin】：{call.from_user.id} 完成删除 {b} 的账户 {e.name}")
    else:
        await editMessage(call, f'🎯 done，等级：{e.lv} - {first.first_name}的账户 {e.name} 删除失败。')
        LOGGER.info(f"【admin】：{call.from_user.id} 对 {b} 的账户 {e.name} 删除失败 ")


@bot.on_callback_query(filters.regex('^fuckoff-'))
async def fuck_off_m(_, call):
    if not judge_admins(call.from_user.id):
        return await call.answer("请不要以下犯上 ok？", show_alert=True)

    await call.answer("✅ ok")
    user_id = int(call.data.split("-")[1])
    if user_id in admins and user_id != call.from_user.id:
        return await editMessage(call,
                                 f"⚠️ 打咩，no，机器人不可以对bot管理员出手喔，请[自己](tg://user?id={call.from_user.id})解决",
                                 timer=60)
    try:
        user = await bot.get_chat(user_id)
        await call.message.chat.ban_member(user_id)  # 默认退群了就删号    fix：call 没有对象chat
        await editMessage(call,
                          f'🎯 done，管理员 [{call.from_user.first_name}](tg://user?id={call.from_user.id}) 已移除 [{user.first_name}](tg://user?id={user_id})[{user_id}]')
        LOGGER.info(
            f"【admin】：{call.from_user.id} 已从群组 {call.message.chat.id} 封禁 {user.first_name} - {user.id}")
    except pyrogram.errors.ChatAdminRequired:
        await editMessage(call,
                          f"⚠️ 请赋予我踢出成员的权限 [{call.from_user.first_name}](tg://user?id={call.from_user.id})")
    except pyrogram.errors.UserAdminInvalid:
        await editMessage(call,
                          f"⚠️ 打咩，no，机器人不可以对群组管理员出手喔，请[自己](tg://user?id={call.from_user.id})解决")


# ──────────────────────────────────────────────────────────────────────
# 单用户并发警告管理：警告 -1 / 重置为 0
#
# 背景：并发检测的警告计数原先只能「整体重置」，遇到误判、或者管理员
# 想对某个用户网开一面时，没法只动一个人的计数。这里把单用户操作挂到
# /kk 面板上（那是既有的单用户管理面板），并复用它的管理员校验。
# 只改 `concurrent_warn_count` 一个字段，不碰封禁状态、不碰 Emby 策略。
# ──────────────────────────────────────────────────────────────────────

async def _refresh_kk_panel(call, uid, warn_count=None):
    """
    重渲染 /kk 面板，让警告数在界面上立刻更新。

    返回 _REFRESH_EDITED / _REFRESH_UNCHANGED / _REFRESH_FAILED。
    调用方要据此决定给管理员看什么提示，所以不能用 True/False 糊在一起 ——
    「面板本来就显示这个值」和「刷新真的失败了」对管理员意味着完全不同的两件事。

    warn_count 由调用方把「刚写入的值」传进来，面板显示的就是我们刚做的改动，
    而不是再查一次库的结果 —— 少一次读取，就少一个「读到的值与写入的值不一致」
    的机会，渲染结果与写入动作严格对应。

    这里刻意不走 editMessage()：它会把 MESSAGE_NOT_MODIFIED / MESSAGE_ID_INVALID
    这类 BadRequest 静默吞掉（只 return False、不写日志），一旦编辑失败就完全
    看不到线索。直接编辑，并把消息 id、聊天 id、渲染出的警告数都记下来，出问题
    才能区分「文本没变」和「消息不可编辑」。
    """
    where = (f"uid={uid} chat={getattr(getattr(call.message, 'chat', None), 'id', None)} "
             f"msg={getattr(call.message, 'id', None)} call={getattr(call, 'id', None)}")

    try:
        first = await bot.get_chat(uid)
        text, keyboard = await cr_kk_ikb(uid, first.first_name, warn_count)
    except Exception as e:
        LOGGER.error(f"重渲染 /kk 面板内容失败 {where}: {type(e).__name__}: {e}")
        return False

    # 编辑；FloodWait 必须自动重试（但有上限）—— 以前走 editMessage() 时它自带
    # 这个重试，自己写编辑就得补上，否则限流下比旧代码更差。
    for attempt in range(2):
        try:
            await call.message.edit(text=text, disable_web_page_preview=True, reply_markup=keyboard)
            LOGGER.info(f"/kk 面板已刷新 {where} 警告数={warn_count}")
            return _REFRESH_EDITED
        except FloodWait as f:
            if f.value > _FLOODWAIT_MAX_WAIT or attempt == 1:
                LOGGER.warning(f"编辑 /kk 面板被限流 {f.value}s，放弃重试，改走兜底 {where}")
                break
            LOGGER.warning(f"编辑 /kk 面板被限流，等待 {f.value}s 后重试 {where}")
            await sleep(f.value * 1.2)
        except BadRequest as e:
            err_id = getattr(e, 'ID', None)
            if err_id == 'MESSAGE_NOT_MODIFIED':
                # 面板本来就是要显示的内容。这恰恰是「点了按钮、提示说改成了、
                # 面板上数字却没动」的现场：面板此前显示的就是改完之后的值
                # （例如库里是 1、面板显示 0，点 -1 后库变 0）。
                # 面板本身是对的，不改发新面板，但必须让调用方知道，
                # 好去向管理员解释「为什么数字没动」。
                LOGGER.info(f"/kk 面板内容与目标一致，无需编辑 {where} 警告数={warn_count}")
                return _REFRESH_UNCHANGED
            LOGGER.error(f"编辑 /kk 面板被 Telegram 拒绝 {where} 警告数={warn_count}: "
                         f"ID={err_id} {e}")
            break
        except Exception as e:
            LOGGER.error(f"编辑 /kk 面板失败 {where}: {type(e).__name__}: {e}")
            break

    # 兜底：编辑不了就改发一条新面板。_send_kk_panel 会顺手删掉同一用户的上一条
    # 面板，所以不会越积越多。宁可换一条消息，也不能出现「数据库改了、提示也弹了、
    # 面板还是旧值」这种管理员完全看不出来的静默失败。
    sent = await _send_kk_panel(call.message.chat.id, uid, text, keyboard)
    if sent is None:
        LOGGER.error(f"改发新面板也失败 {where} 警告数={warn_count}")
        return _REFRESH_FAILED
    # _send_kk_panel 删的是「跟踪到的那条」，可能不是管理员点的这条（例如进程重启后
    # 跟踪表已清空）。这里补删一次；对同一个 id 重复删除是幂等的，删不掉也无所谓。
    if getattr(call.message, 'id', None) != sent.id:
        try:
            await call.message.delete()
        except Exception:
            pass
    LOGGER.warning(f"原面板无法编辑，已改发新面板 {where} 警告数={warn_count} 新msg={sent.id}")
    return _REFRESH_EDITED


async def sync_kk_panel(uid=None):
    """
    警告数被别处（并发检测 +1 / 全局重置）改动后，把已打开的 /kk 面板同步刷新。

    不做这一步，面板就会停在改动前的值：管理员看到 0、库里其实是 1，点
    「➖ 警告-1」写回 0 后面板文本与当前消息一字不差，Telegram 会以
    MESSAGE_NOT_MODIFIED 拒绝编辑 —— 现象正是「提示说改成了，数字却没动」。

    纯尽力而为：任何失败只记日志，绝不向上抛。调用方是每 60 秒跑一次的并发
    检测任务，不能被这里拖垮。uid 为 None 表示同步所有已记录的面板。
    """
    targets = [(cid, u, mid) for (cid, u), mid in list(_kk_panel_ids.items())
               if uid is None or u == uid]
    if not targets:
        return
    for chat_id, target_uid, msg_id in targets:
        try:
            first = await bot.get_chat(target_uid)
            text, keyboard = await cr_kk_ikb(target_uid, first.first_name, None)
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text,
                                        disable_web_page_preview=True, reply_markup=keyboard)
            LOGGER.debug(f"已同步 /kk 面板 uid={target_uid} chat={chat_id} msg={msg_id}")
        except BadRequest as e:
            err_id = getattr(e, 'ID', None)
            if err_id == 'MESSAGE_NOT_MODIFIED':
                continue  # 内容本来就一致，正常情况
            # 面板已被删除 / 已不可编辑：停止跟踪，免得每 60 秒白试一次
            LOGGER.info(f"/kk 面板已不可同步（{err_id}），停止跟踪 "
                        f"uid={target_uid} chat={chat_id} msg={msg_id}")
            _kk_panel_ids.pop((chat_id, target_uid), None)
        except FloodWait as f:
            LOGGER.warning(f"同步 /kk 面板被限流 {f.value}s，本次跳过 uid={target_uid}")
        except Exception as e:
            LOGGER.warning(f"同步 /kk 面板失败 uid={target_uid} chat={chat_id}: "
                           f"{type(e).__name__}: {e}")


def _load_warn_state(uid):
    """返回 (emby记录, 当前警告数, 阈值)；用户不存在时第一个元素为 None。"""
    e = sql_get_emby(uid)
    if e is None:
        return None, 0, config.concurrent_play_warn_threshold
    return e, int(e.concurrent_warn_count or 0), config.concurrent_play_warn_threshold


async def _apply_warn_change(call, uid, new_value, action_desc):
    """写入新的警告数并刷新面板；面板刷新失败要让管理员看得见。"""
    if sql_update_emby(Emby.tg == uid, concurrent_warn_count=new_value) is not True:
        LOGGER.error(f"【admin】：{call.from_user.id} 调整 {uid} 并发警告数失败（数据库写入错误）")
        await call.answer("⚠️ 数据库写入失败，请查看日志", show_alert=True)
        return False
    LOGGER.info(f"【admin】：{call.from_user.id} 将 {uid} 的并发警告数调整为 {new_value}")

    # 必须先刷新、再应答，而且**只能应答一次**：Telegram 的回调应答是一次性的，
    # 「先答成功、失败后再补一条告警」是无效设计 —— 第二条会被以
    # QUERY_ID_INVALID / "query is too old" 拒绝，管理员就只看到「✅ 已改」，
    # 正是要修的那个静默失败形态。
    result = await _refresh_kk_panel(call, uid, new_value)
    if result == _REFRESH_EDITED:
        await call.answer(action_desc)
    elif result == _REFRESH_UNCHANGED:
        # 面板本来就显示这个值 —— 数据是对的，但要解释「为什么数字没动」
        await call.answer(f"{action_desc}；面板此前已显示该值，故外观未变")
    else:
        await call.answer("⚠️ 计数已写入数据库，但面板刷新失败，请重新 /kk 查看", show_alert=True)
    return result != _REFRESH_FAILED


@bot.on_callback_query(filters.regex('^warn_minus-'))
async def kk_warn_minus(_, call):
    if not judge_admins(call.from_user.id):
        return await call.answer("请不要以下犯上 ok？", show_alert=True)

    try:
        uid = int(call.data.split("-")[1])
    except (IndexError, ValueError):
        return await call.answer("❌ 数据格式错误", show_alert=True)

    e, cur, _threshold = _load_warn_state(uid)
    if e is None:
        return await call.answer("💢 ta 没有注册账户。", show_alert=True)
    if cur <= 0:
        await call.answer("当前警告数已经是 0，无需再减", show_alert=True)
        return await _refresh_kk_panel(call, uid, cur)

    new_value = cur - 1
    await _apply_warn_change(call, uid, new_value, f"✅ 警告数 {cur} → {new_value}")


@bot.on_callback_query(filters.regex('^warn_reset-'))
async def kk_warn_reset(_, call):
    if not judge_admins(call.from_user.id):
        return await call.answer("请不要以下犯上 ok？", show_alert=True)

    try:
        uid = int(call.data.split("-")[1])
    except (IndexError, ValueError):
        return await call.answer("❌ 数据格式错误", show_alert=True)

    e, cur, _threshold = _load_warn_state(uid)
    if e is None:
        return await call.answer("💢 ta 没有注册账户。", show_alert=True)
    if cur == 0:
        await call.answer("当前警告数已经是 0", show_alert=True)
        return await _refresh_kk_panel(call, uid, cur)

    await _apply_warn_change(call, uid, 0, f"✅ 警告数已重置为 0（原 {cur}）")
