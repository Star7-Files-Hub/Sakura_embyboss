"""
kk - 纯装x
赠与账户，禁用，删除
"""
import pyrogram
from pyrogram import filters
from pyrogram.errors import BadRequest
from bot import bot, prefixes, owner, admins, LOGGER, extra_emby_libs, config
from bot.func_helper.emby import emby
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.fix_bottons import cr_kk_ikb, gog_rester_ikb
from bot.func_helper.msg_utils import deleteMessage, sendMessage, editMessage, escape_markdown
from bot.func_helper.utils import judge_admins, cr_link_two, tem_deluser
from bot.sql_helper.sql_emby import sql_add_emby, sql_get_emby, sql_update_emby, Emby


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
            await sendMessage(msg, text=text, buttons=keyboard)  # protect_content=True 移除禁止复制

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
        await sendMessage(msg, text=text, buttons=keyboard)


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

    warn_count 必须由调用方把「刚写入的值」传进来：面板文本要反映的是
    我们刚做的改动，而不是再查一次库的结果。若二次读取拿回旧值，渲染出的
    文本会与当前消息完全相同，Telegram 会以 MESSAGE_NOT_MODIFIED 拒绝编辑，
    现象就是「操作成功了但面板不刷新」。

    这里也刻意不走 editMessage()：它会把 MESSAGE_NOT_MODIFIED /
    MESSAGE_ID_INVALID 这类 BadRequest 静默吞掉（只 return False、不写日志），
    一旦编辑失败就完全看不到线索。直接编辑并把结果与具体错误 ID 都记下来。
    """
    try:
        first = await bot.get_chat(uid)
        text, keyboard = await cr_kk_ikb(uid, first.first_name, warn_count)
    except Exception as e:
        LOGGER.error(f"重渲染 /kk 面板内容失败 uid={uid}: {type(e).__name__}: {e}")
        return False

    try:
        await call.message.edit(text=text, disable_web_page_preview=True, reply_markup=keyboard)
        LOGGER.info(f"/kk 面板已刷新 uid={uid} 警告数={warn_count}")
        return True
    except BadRequest as e:
        err_id = getattr(e, 'ID', '?')
        if err_id == 'MESSAGE_NOT_MODIFIED':
            # 面板内容本来就与要渲染的一致，属于正常情况，不需要任何处理
            LOGGER.info(f"/kk 面板内容未变化，跳过编辑 uid={uid} 警告数={warn_count}")
            return True
        LOGGER.error(f"编辑 /kk 面板被 Telegram 拒绝 uid={uid} 警告数={warn_count}: ID={err_id} {e}")
    except Exception as e:
        LOGGER.error(f"编辑 /kk 面板失败 uid={uid}: {type(e).__name__}: {e}")

    # 兜底：编辑不了就改发一条新面板。
    # 宁可多一条消息，也不能出现「数据库改了、提示也弹了，但面板还是旧值」
    # 这种管理员完全看不出来的静默失败。
    try:
        await bot.send_message(chat_id=call.message.chat.id, text=text,
                               disable_web_page_preview=True, reply_markup=keyboard)
        LOGGER.warning(f"原面板无法编辑，已改发新面板 uid={uid} 警告数={warn_count}")
        try:
            await call.message.delete()
        except Exception:
            pass  # 删不掉旧的就留着，不影响正确性
        return True
    except Exception as e:
        LOGGER.error(f"改发新面板也失败 uid={uid}: {type(e).__name__}: {e}")
        return False


def _load_warn_state(uid):
    """返回 (emby记录, 当前警告数, 阈值)；用户不存在时第一个元素为 None。"""
    e = sql_get_emby(uid)
    if e is None:
        return None, 0, config.concurrent_play_warn_threshold
    return e, int(e.concurrent_warn_count or 0), config.concurrent_play_warn_threshold


async def _apply_warn_change(call, uid, new_value, action_desc):
    """写入新的警告数并刷新面板。"""
    if sql_update_emby(Emby.tg == uid, concurrent_warn_count=new_value) is not True:
        LOGGER.error(f"【admin】：{call.from_user.id} 调整 {uid} 并发警告数失败（数据库写入错误）")
        await call.answer("⚠️ 数据库写入失败，请查看日志", show_alert=True)
        return False
    await call.answer(action_desc)
    LOGGER.info(f"【admin】：{call.from_user.id} 将 {uid} 的并发警告数调整为 {new_value}")
    await _refresh_kk_panel(call, uid, new_value)
    return True


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
