#! /usr/bin/python3
# -*- coding: utf-8 -*-

from asyncio import sleep

import asyncio

from pathlib import Path
from pyrogram import filters, enums
from pyrogram.errors import FloodWait, Forbidden, BadRequest, PeerIdInvalid
from pyrogram.types import CallbackQuery
from pyromod.exceptions import ListenerTimeout 
from bot import LOGGER, group, bot
from typing import Optional

LOCAL_PHOTO_FALLBACK = Path(__file__).resolve().parents[2] / "image" / "bot2.png"

PHOTO_SEND_FALLBACK_ERROR_IDS = {
    "PHOTO_INVALID",
    "PHOTO_EXT_INVALID",
    "PHOTO_FILE_INVALID",
    "PHOTO_INVALID_DIMENSIONS",
    "WEBPAGE_MEDIA_EMPTY",
    "WEBPAGE_CURL_FAILED",
    "MEDIA_EMPTY",
    "FILE_REFERENCE_EXPIRED",
}


def _is_photo_send_error(error: Exception) -> bool:
    error_id = getattr(error, "ID", "")
    error_text = str(error).upper()
    return (
        error_id in PHOTO_SEND_FALLBACK_ERROR_IDS
        or "PHOTO" in error_text
        or "WEBPAGE" in error_text
        or "FILE_REFERENCE" in error_text
        or "MEDIA_EMPTY" in error_text
        or "FAILED TO DECODE" in error_text
        or "VALID FILE ID" in error_text
        or "EXISTING LOCAL FILE" in error_text
    )


async def _send_photo_payload(message, photo, caption=None, buttons=None, timer=None, send=False, chat_id=None):
    if send is True:
        if chat_id is None:
            chat_id = group[0]
        return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, reply_markup=buttons)

    sent = await message.reply_photo(photo=photo, caption=caption, disable_notification=True, reply_markup=buttons)
    if timer is not None:
        return await deleteMessage(sent, timer)
    return True


async def _send_photo_text_fallback(message, caption=None, buttons=None, timer=None, send=False, chat_id=None):
    text = caption or "图片发送失败，暂无可显示内容。"
    if send is True:
        if chat_id is None:
            chat_id = group[0]
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=buttons)

    sent = await bot.send_message(chat_id=message.chat.id, text=text, reply_markup=buttons)
    if timer is not None:
        return await deleteMessage(sent, timer)
    return True


async def _send_local_photo_fallback(message, caption=None, buttons=None, timer=None, send=False, chat_id=None):
    if not LOCAL_PHOTO_FALLBACK.exists():
        LOGGER.warning(f"本地默认图片不存在，已降级为文本消息: {LOCAL_PHOTO_FALLBACK}")
        return await _send_photo_text_fallback(message, caption, buttons, timer, send, chat_id)

    try:
        LOGGER.warning(f"图片发送失败，尝试使用本地默认图片: {LOCAL_PHOTO_FALLBACK}")
        return await _send_photo_payload(
            message,
            str(LOCAL_PHOTO_FALLBACK),
            caption,
            buttons,
            timer,
            send,
            chat_id,
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(f.value * 1.2)
        return await _send_local_photo_fallback(message, caption, buttons, timer, send, chat_id)
    except Exception as e:
        LOGGER.error(f"本地默认图片发送失败，已降级为文本消息: {e}")
        return await _send_photo_text_fallback(message, caption, buttons, timer, send, chat_id)


async def warmup_peer_cache():
    """bot 重启后预热 peer 缓存。

    bot.get_chat() 内部调用 resolve_peer，后者直接查询 session SQLite，
    如果该群的 access_hash 已持久化（bot 之前收过该群的更新），则可正常预热。
    对于从未在 session 中出现过的群组，bot 无法主动获取 access_hash，
    需等待 Telegram 推送第一条更新后自动写入 session 并恢复正常。
    """
    for gid in group:
        try:
            await bot.get_chat(gid)
            LOGGER.info(f"peer 预热成功: {gid}")
        except PeerIdInvalid:
            LOGGER.warning(f"peer 预热跳过 (gid={gid}): peer 不在 session 中，待群内有首条更新后自动写入并恢复")
        except Exception as e:
            LOGGER.warning(f"peer 预热失败 (gid={gid}): {e}")

# 将来自己要是重写，希望不要把/cancel当关键词，用call.data，省代码还好看，切记。

async def sendMessage(message, text: str, buttons=None, timer=None, send=False, chat_id=None, parse_mode: Optional["enums.ParseMode"] = None):
    """
    发送消息
    :param message: 消息
    :param text: 实体
    :param buttons: 按钮
    :param timer: 定时删除
    :param send: 非reply,发送到第一个主授权群组
    :return:
    """
    if isinstance(message, CallbackQuery):
        message = message.message
    try:
        if send is True:
            if chat_id is None:
                chat_id = group[0]
            return await bot.send_message(chat_id=chat_id, text=text, reply_markup=buttons, parse_mode=parse_mode)
        # 禁用通知 disable_notification=True,
        send = await message.reply(text=text, quote=True, disable_web_page_preview=True, reply_markup=buttons)
        if timer is not None:
            return await deleteMessage(send, timer)
        return True
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(f.value * 1.2)
        return await sendMessage(message, text, buttons, parse_mode=parse_mode)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


def _edit_target_of(message) -> str:
    """日志用的「编辑目标」描述；任何属性取不到都不抛异常。"""
    try:
        chat_id = getattr(getattr(message, 'chat', None), 'id', None)
        return f"chat={chat_id} msg={getattr(message, 'id', None)}"
    except Exception:
        return "chat=? msg=?"


async def editMessage(message, text: str, buttons=None, timer=None, parse_mode: Optional["enums.ParseMode"] = None):
    """
    编辑消息
    :param message:
    :param text:
    :param buttons:
    :return:
    """
    if isinstance(message, CallbackQuery):
        message = message.message
    try:
        edt = await message.edit(text=text, disable_web_page_preview=True, reply_markup=buttons, parse_mode=parse_mode)
        if timer is not None:
            return await deleteMessage(edt, timer)
        return True
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(f.value * 1.2)
        return await editMessage(message, text, buttons, parse_mode=parse_mode)
    except BadRequest as e:
        # 下面三条以前是彻底静默的（只 return False，一行日志都不写），
        # 结果「界面没更新」时完全拿不到线索，只能靠猜。至少留下可检索的记录：
        # MESSAGE_NOT_MODIFIED 记 INFO（内容与目标一致，通常是正常情况，但
        # 也可能是管理员对着一条陈旧面板操作，需要能查到），另外两条记 WARNING。
        where = _edit_target_of(message)
        if e.ID == 'BUTTON_URL_INVALID':
            # await editMessage(message, text='⚠️ 底部按钮设置失败。', buttons=back_start_ikb)
            LOGGER.warning(f"editMessage 失败[{e.ID}] {where}: 底部按钮被 Telegram 拒绝")
            return False
        # 判断是否是因为编辑到一样的消息
        if e.ID == "MESSAGE_NOT_MODIFIED" or e.ID == 'MESSAGE_ID_INVALID':
            # await callAnswer(message, "慢速模式开启，切勿多点\n慢一点，慢一点，生活更有趣 - zztai", True)
            LOGGER.info(f"editMessage 未生效[{e.ID}] {where}")
            return False
        else:
            # 记录或处理其他异常
            LOGGER.warning(f"editMessage 失败[{e.ID}] {where}: {e}")
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def sendFile(message, file, file_name, caption=None, buttons=None):
    """
    发送文件
    :param message:
    :param file:
    :param file_name:
    :param caption:
    :param buttons:
    :return:
    """
    if isinstance(message, CallbackQuery):
        message = message.message
    try:
        await message.reply_document(document=file, file_name=file_name, quote=False, caption=caption,
                                     reply_markup=buttons)
        return True
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(f.value * 1.2)
        # L-3：重试时必须把 file_name/caption/buttons 一并传回，否则参数错位
        return await sendFile(message, file, file_name, caption, buttons)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def sendPhoto(message, photo, caption=None, buttons=None, timer=None, send=False, chat_id=None):
    """
    发送图片
    :param message:
    :param photo:
    :param caption:
    :param buttons:
    :param timer:
    :param send: 是否发送到授权主群
    :return:
    """
    if isinstance(message, CallbackQuery):
        message = message.message
    try:
        return await _send_photo_payload(message, photo, caption, buttons, timer, send, chat_id)
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(f.value * 1.2)
        return await sendPhoto(message, photo, caption, buttons, timer, send, chat_id)
    except BadRequest as e:
        if _is_photo_send_error(e):
            LOGGER.warning(f"图片发送失败，将尝试本地默认图片: {e}")
            return await _send_local_photo_fallback(message, caption, buttons, timer, send, chat_id)
        LOGGER.error(str(e))
        return str(e)
    except Exception as e:
        if _is_photo_send_error(e):
            LOGGER.warning(f"图片发送失败，将尝试本地默认图片: {e}")
            return await _send_local_photo_fallback(message, caption, buttons, timer, send, chat_id)
        LOGGER.error(str(e))
        return str(e)


async def deleteMessage(message, timer=None):
    """
    删除消息,带定时
    :param message:
    :param timer:
    :return:
    """
    if timer is not None:
        await asyncio.sleep(timer)
    if isinstance(message, CallbackQuery):
        try:
            await message.message.delete()
            return await callAnswer(message, '✔️ Done!')  # 返回 True 表示删除成功
        except FloodWait as f:
            LOGGER.warning(str(f))
            await asyncio.sleep(f.value * 1.2)
            return await deleteMessage(message, timer)  # 重新调用自己的函数
        except Forbidden as e:
            await callAnswer(message, f'⚠️ 消息已过期，请重新 唤起面板\n/start', True)
        except BadRequest as e:
            pass
        except Exception as e:
            LOGGER.error(e)
            return str(e)  # 返回异常字符串表示删除出错
    else:
        try:
            await message.delete()
            return True  # 返回 True 表示删除成功
        except FloodWait as f:
            LOGGER.warning(str(f))
            await asyncio.sleep(f.value * 1.2)
            return await deleteMessage(message, timer)  # 重新调用自己的函数
        except Forbidden as e:
            LOGGER.warning(e)
            await message.reply(f'⚠️ **错误！**检查群组 `{message.chat.id}` 权限 【删除消息】')
            # return await deleteMessage(send, 60)
        except BadRequest as e:
            pass
        except Exception as e:
            LOGGER.error(e)
            return str(e)  # 返回异常字符串表示删除出错


async def callAnswer(callbackquery: CallbackQuery, query, show_alert=False):
    try:
        await callbackquery.answer(query, show_alert=show_alert)
        return True
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(f.value * 1.2)
        # 递归地调用自己的函数
        return await callAnswer(callbackquery, query, show_alert)
    except BadRequest as e:
        # 判断异常的消息是否是 "Query_id_invalid"
        if e.ID == "QUERY_ID_INVALID":
            # 忽略这个异常
            return False
        else:
            LOGGER.error(str(e))
            return False
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def callListen(callbackquery, timer: int = 120, buttons=None):
    try:
        # M-4：pyromod 的 Chat.listen 绑定在 chat 上，群聊里会把任意成员的消息当成发起者的输入。
        # 这里限定只接收回调发起者本人的文本消息。
        return await callbackquery.message.chat.listen(
            filters.text & filters.user(callbackquery.from_user.id), timeout=timer)
    except ListenerTimeout:
        await editMessage(callbackquery, '💦 __没有获取到您的输入__ **会话状态自动取消！**', buttons=buttons)
        return False


async def call_dice_listen(callbackquery, timer: int = 120, buttons=None):
    try:
        # M-4：同上，限定只接收回调发起者本人的骰子消息
        return await callbackquery.message.chat.listen(
            filters.dice & filters.user(callbackquery.from_user.id), timeout=timer)
    except ListenerTimeout:
        await editMessage(callbackquery, '💦 __没有获取到您的输入__ **会话状态自动取消！**', buttons=buttons)
        return False


async def callAsk(callbackquery, text, timer: int = 120, button=None):
    # 使用ask方法发送一条消息，并等待用户的回复，最多120秒，只接受文本类型的消息
    try:
        txt = await callbackquery.message.chat.ask(text, filters=filters.CallbackQuery, timeout=timer, button=button)
        return True
    except:
        return False


async def ask_return(update, text, timer: int = 120, button=None):
    if isinstance(update, CallbackQuery):
        update = update.message
    try:
        return await update.chat.ask(text=text, timeout=timer)
    except ListenerTimeout:
        await sendMessage(update, '💦 __没有获取到您的输入__ **会话状态自动取消！**', buttons=button)
        return None


import re
import html


# 转义特殊字符
def escape_html_special_chars(text):
    # 定义一些常用的字符
    pattern = r"[\\`*_{}[\]()#+-.!|]"
    # 使用正则表达式替换掉特殊字符
    text = re.sub(pattern, r"\\\g<0>", text)
    # 使用html模块转义HTML的特殊字符
    text = html.escape(text)
    return text


def escape_markdown(text):
    """转义 Markdown(legacy, ParseMode.MARKDOWN) 的特殊字符。

    L-4：bot 全局使用 ParseMode.MARKDOWN（旧版 Markdown），其需要转义的字符
    仅为 `_`、`*`、`` ` ``、`[`、`]`（与 python-telegram-bot 的 v1 转义集合一致）。
    原先的字符集是 MarkdownV2 的集合，会对 `.`/`-`/`!` 等普通字符加反斜杠，
    在旧版 Markdown 下可能被原样显示，故收敛为旧版 Markdown 的字符集。
    """
    return (
        re.sub(r"([_*`\[\]])", r"\\\1", html.unescape(text))
        if text
        else str()
    )
