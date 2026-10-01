"""
 一个群组检测，防止别人把bot拉过去，而刚好代码出现漏洞。
"""
import asyncio

from pyrogram import filters

from bot import bot, group, owner, LOGGER
from bot.func_helper.fix_bottons import dp_g_ikb
from bot.func_helper.msg_utils import escape_markdown

# 定义一个集合来存储已经处理过的群组的 id
processed_groups = set()


async def leave_bot(chat_id):
    await asyncio.sleep(30)
    try:
        # 踢出bot
        await bot.leave_chat(chat_id)
        LOGGER.info(f"bot已 退出未授权群聊【{chat_id}】")
    except Exception as e:
        # 记录异常信息
        LOGGER.error(e)
    finally:
        # L-2：退出后释放标记，避免 processed_groups 只增不减（长期运行内存单调增长）
        processed_groups.discard(chat_id)


@bot.on_message(~filters.chat(group) & filters.group)
async def anti_use_bot(_, msg):
    if msg.chat.id in processed_groups:
        return
    else:
        processed_groups.add(msg.chat.id)
    if msg.from_user is not None:
        try:
            await bot.send_message(owner,
                                   f"[{escape_markdown(msg.from_user.first_name)}](tg://user?id={msg.from_user.id})"
                                   f"[`{msg.from_user.id}`]试图将bot拉入 `{msg.chat.id}` 已被发现")
            asyncio.create_task(leave_bot(msg.chat.id))
            await bot.send_message(msg.chat.id,
                                   f'❎ 这并非一个授权群组！！！[`{msg.chat.id}`]\n\n本bot将在 **30s** 自动退出如有疑问请联系开发👇',
                                   reply_markup=dp_g_ikb)
            LOGGER.info(f"【[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})"
                        f"[`{msg.from_user.id}`]试图将bot拉入 `{msg.chat.id}` 已被发现】")
        except Exception as e:
            # 记录异常信息
            LOGGER.error(e)

    elif msg.from_user is None:
        try:
            await bot.send_message(chat_id=owner, text=f'有坏蛋 试图将bot拉入 `{msg.chat.id}` 已被发现')
            asyncio.create_task(leave_bot(msg.chat.id))
            await bot.send_message(msg.chat.id,
                                   f'❎ 这并非一个授权群组！！！[`{msg.chat.id}`]\n\n本bot将在 **30s** 自动退出如有疑问请联系开发👇',
                                   reply_markup=dp_g_ikb)
            LOGGER.info(f"【有坏蛋试图将bot拉入 `{msg.chat.id}` 已被发现】")
        except Exception as e:
            # 记录异常信息
            LOGGER.error(e)
