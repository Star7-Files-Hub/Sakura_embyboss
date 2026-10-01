from pyrogram import filters

from bot import bot, prefixes, LOGGER
from bot.func_helper.emby import emby
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.msg_utils import deleteMessage, editMessage, sendMessage
from bot.func_helper.utils import tem_deluser
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby, sql_delete_emby_by_tg, sql_delete_emby
from bot.sql_helper.sql_emby2 import sql_get_emby2, sql_delete_emby2_by_name


# 删除账号命令
@bot.on_message(filters.command('rmemby', prefixes) & admins_on_filter)
async def rmemby_user(_, msg):
    await deleteMessage(msg)
    reply = await msg.reply("🍉 正在处理ing....")
    if msg.reply_to_message is None:
        try:
            b = msg.command[1]  # name
        except (IndexError, KeyError, ValueError):
            return await editMessage(reply,
                                     "🔔 **使用格式：**/rmemby tg_id或回复某人 \n/rmemby [emby用户名亦可]")
        e = sql_get_emby(tg=b)
    else:
        b = msg.reply_to_message.from_user.id
        e = sql_get_emby(tg=b)

    if e is None:
        return await reply.edit(f"♻️ 没有检索到 {b} 账户，请确认重试或手动检查。")

    if e.embyid is not None:
        first = await bot.get_chat(e.tg)
        sign_name = f'{msg.sender_chat.title}' if msg.sender_chat else f'[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'
        if await emby.emby_del(emby_id=e.embyid):
            # 远端账号已删除，名额计数照常回收
            tem_deluser()
            # B-H5：检查数据库更新返回值；条件改用主键 tg，避免 NULL 比较误命中（B-L3）
            if not sql_update_emby(Emby.tg == e.tg, embyid=None, name=None, pwd=None, pwd2=None, lv='d', cr=None, ex=None):
                LOGGER.error(f"管理员 {sign_name} 删除远端账号成功，但数据库更新失败: tg={e.tg}")
                await reply.edit(f"⚠️ Emby 账号已删除，但数据库记录更新失败，请用 `/only_rm_record {e.tg}` 手工清理。")
                return
            try:
                await reply.edit(
                    f'🎯 done，管理员  {sign_name} 已将 [{first.first_name}](tg://user?id={e.tg}) 账户 {e.name} 删除。')
                await bot.send_message(e.tg,
                                       f'🎯 done，管理员 {sign_name} 已将 您的账户 {e.name} 删除。')
            except:
                pass
            LOGGER.info(
                f"【admin】：管理员 {sign_name} 执行删除 {first.first_name}-{e.tg} 账户 {e.name}")
    else:
        await reply.edit(f"💢 [ta](tg://user?id={b}) 还没有注册账户呢")
@bot.on_message(filters.command('only_rm_record', prefixes) & admins_on_filter)
async def only_rm_record(_, msg):
    await deleteMessage(msg)
    tg_id = None
    if msg.reply_to_message is None:
        try:
            tg_id = msg.command[1]
        except (IndexError, ValueError):
            tg_id = None
    else:
        tg_id = msg.reply_to_message.from_user.id
    if tg_id is None:
        return await sendMessage(msg, "❌ 使用格式：/only_rm_record tg_id或回复用户的消息")

    emby1 = sql_get_emby(tg=tg_id)
    # 获取 emby2 表中的用户信息
    emby2 = sql_get_emby2(name=tg_id)
    if not emby1 and not emby2:
        return await sendMessage(msg, f"❌ 未找到 {tg_id} 的记录")
    try:
        res1 = False
        res2 = False
        if emby1:
            res1 = sql_delete_emby_by_tg(tg_id)
        if emby2:
            res2 = sql_delete_emby2_by_name(name=tg_id)
        sign_name = f'{msg.sender_chat.title}' if msg.sender_chat else f'[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'
        if res1 or res2:
            await sendMessage(msg, f"管理员 {sign_name} 已删除 TG ID: {tg_id} 的数据库记录")
            LOGGER.info(
                f"管理员 {sign_name} 删除了用户 {tg_id} 的数据库记录")
        else:
            await sendMessage(msg, "❌ 删除记录失败")
            LOGGER.error(
                f"管理员 {sign_name} 删除用户 {tg_id} 的数据库记录失败")
    except Exception as ex:
        await sendMessage(msg, "❌ 删除记录失败")
        LOGGER.error(f"删除用户 {tg_id} 的数据库记录失败, {ex}")


@bot.on_message(filters.command('only_rm_emby', prefixes) & admins_on_filter)
async def only_rm_emby(_, msg):
    await deleteMessage(msg)
    try:
        emby_id = msg.command[1]
        confirm = msg.command[2] if len(msg.command) > 2 else None
    except (IndexError, ValueError):
        return await sendMessage(msg, "❌ 使用格式：/only_rm_emby embyid [true]")

    # B-M8：不可恢复的删除操作加显式确认（与 /paolu /banall 等危险命令一致）
    if confirm != 'true':
        return await sendMessage(
            msg,
            "⚠️ 此操作将直接删除 Emby 账号（不可恢复，且不会自动清理数据库记录）。\n"
            f"如确定请使用 `/only_rm_emby {emby_id} true`")

    sign_name = f'{msg.sender_chat.title}' if msg.sender_chat else f'[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'

    # B-M8：仅当确认该 ID 不存在（404）时才按用户名兜底，避免"删除失败（网络/服务异常）后误删同名账号"
    exists, info = await emby.user(emby_id=emby_id)
    if not exists:
        error_text = info.get('error', '') if isinstance(info, dict) else str(info)
        if '资源不存在' not in error_text:
            return await sendMessage(msg, f"❌ 无法确认 Emby 账号 {emby_id} 是否存在：{error_text}")
        success, embyuser = await emby.get_emby_user_by_name(emby_name=emby_id)
        if not success:
            return await sendMessage(msg, f"❌ 未找到此用户 {emby_id} 的记录")
        emby_id = embyuser.get("Id")

    if not await emby.emby_del(emby_id=emby_id):
        return await sendMessage(msg, f"❌ 删除用户 {emby_id} 失败（可能是 Emby 服务异常），请稍后重试")
    await sendMessage(msg, f"管理员 {sign_name} 已删除用户 {emby_id} 的Emby账号")
    LOGGER.info(f"管理员 {sign_name} 删除了用户 {emby_id} 的Emby账号")