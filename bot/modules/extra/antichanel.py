"""
antichannel - 恶趣味，因为我没有所以其他人也不行。阿门

Author:susu
Date:2023/12/30
"""
import asyncio

from pyrogram import filters

from bot import bot, prefixes, w_anti_channel_ids, LOGGER, save_config, config
from bot.func_helper.filters import admins_on_filter


async def get_user_input(msg):
    await msg.delete()
    gm = msg.sender_chat.title if msg.sender_chat else f'管理员 [{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'
    if msg.reply_to_message is None:
        try:
            chatid = int(msg.command[1])
        except (IndexError, KeyError, ValueError, AttributeError):
            return None, gm
    else:
        # 被回复的消息可能来自普通用户，此时 sender_chat 为 None（原实现会抛 AttributeError）
        sender_chat = msg.reply_to_message.sender_chat
        if sender_chat is None:
            return None, gm
        chatid = sender_chat.id
    # 皮套人只可能是频道/群组身份，其 chat id 必为负数。
    # 拒绝正数（用户 id），避免 /unban_channel、/rev_white_channel 被用来对任意用户执行封禁/解禁。
    if not isinstance(chatid, int) or chatid >= 0:
        return None, gm
    return chatid, gm


@bot.on_message(filters.command('unban_channel', prefixes) & admins_on_filter)
async def un_fukk_pitao(_, msg):
    a, gm = await get_user_input(msg)
    if not a:
        return await msg.reply('使用 /unban_channel 回复 或 /unban_channel + [id/用户名] 为皮套解禁')
    await asyncio.gather(msg.chat.unban_member(a), msg.reply(f'🕶️ {gm} 解禁皮套 ——> {a}'))
    LOGGER.info(f'【AntiChannel】- {gm} 解禁皮套 ——> {a} ')


@bot.on_message(filters.command('white_channel', prefixes) & admins_on_filter)
async def allow_pitao(_, msg):
    chatid, gm = await get_user_input(msg)
    if not chatid:
        return await msg.reply('使用 /white_channel 回复 或 /white_channel + [id/用户名] 加入皮套人白名单')
    if chatid not in w_anti_channel_ids:
        w_anti_channel_ids.append(chatid)
        save_config()
    await asyncio.gather(msg.reply(f'🎁 {gm} 已为 {chatid} 添加皮套人白名单'), msg.chat.unban_member(chatid))
    LOGGER.info(f'【AntiChannel】- {gm} 豁免皮套 ——> {chatid}')


@bot.on_message(filters.command('rev_white_channel', prefixes) & admins_on_filter)
async def remove_pitao(_, msg):
    a, gm = await get_user_input(msg)
    if not a:
        return await msg.reply('使用 /rev_white_channel 回复 或 /rev_white_channel + [id/用户名] 移除皮套人白名单')
    if a in w_anti_channel_ids:
        w_anti_channel_ids.remove(a)
        save_config()
    await asyncio.gather(msg.reply(f'🕶️ {gm} 已为 {a} 移除皮套人白名单并封禁'), msg.chat.ban_member(a))
    LOGGER.info(f'【AntiChannel】- {gm} 封禁皮套 ——> {a}')


custom_message_filter = filters.create(
    lambda _, __, message: False if message.forward_from_chat or message.from_user or not config.fuxx_pitao else True)
custom_chat_filter = filters.create(
    lambda _, __,
           message: bool(message.sender_chat
                         and message.sender_chat.id != message.chat.id
                         and message.sender_chat.id not in w_anti_channel_ids))


@bot.on_message(custom_message_filter & custom_chat_filter & filters.group)
async def fuxx_pitao(_, msg):
    # 如果开启了狙杀皮套人功能
    # if config.fuxx_pitao:
    try:
        await asyncio.gather(msg.delete(),
                             msg.reply(f'🎯 自动狙杀皮套人！{msg.sender_chat.title} - `{msg.sender_chat.id}`'))
        await msg.chat.ban_member(msg.sender_chat.id)
        LOGGER.info(
            f'【AntiChannel】- {msg.sender_chat.title} - {msg.sender_chat.id} 被封禁')
    except Exception as e:
        # 原实现静默吞掉异常，导致自动狙杀失败时没有任何线索
        LOGGER.error(f'【AntiChannel】- 自动狙杀皮套人失败: {e}')
