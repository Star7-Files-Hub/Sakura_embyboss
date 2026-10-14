#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
一键复制（copy_text 按钮）支持。

为什么需要
----------
Bot API 7.11 起 InlineKeyboardButton 支持 copy_text：用户点一下按钮，客户端就把
指定文本复制到剪贴板。而 Telegram iOS 不支持「点等宽文字即复制」（这个行为只在
Android/桌面版有，Telegram-iOS#1454 至今 open），所以服务器面板里的密码在 iPhone
上点不动 —— 只能用这个按钮实现一键复制。

为什么要走 HTTP Bot API
-----------------------
本项目用的 Pyrogram 2.0.106 的 TL schema 里没有 KeyboardButtonCopy，reply_markup
无法表达 copy_text 字段。因此只对「需要该按钮的那一条消息」直接调 HTTP Bot API：
正文与实体仍由 pyrogram 自己的解析器生成（与 MTProto 路径完全一致，实体偏移量
本来就是 Bot API 要求的 UTF-16 单位），其余逻辑与 editMessage 保持一致。
任何一步失败都回退到 editMessage，行为不劣化。

用法
----
    await editMessageWithCopy(call, text, buttons=keyboard, copy_text=pwd)
"""
from __future__ import annotations

import aiohttp
from pyrogram import enums
from pyrogram.parser import Parser
from pyrogram.types import CallbackQuery

from bot import LOGGER, bot_token
from bot.func_helper.msg_utils import deleteMessage, editMessage

API_ROOT = "https://api.telegram.org"
HTTP_TIMEOUT = 20

COPY_BUTTON_LABEL = "🔑 复制密码"
COPY_TEXT_MAX = 256

# pyrogram 解析出的实体类名 → Bot API 的 MessageEntity type
_ENTITY_TYPES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityUrl": "url",
    "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone_number",
    "MessageEntityMention": "mention",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityBotCommand": "bot_command",
    "MessageEntityMentionName": "text_mention",
    "MessageEntityCustomEmoji": "custom_emoji",
}

# 判断「这条消息带媒体（正文是 caption 而不是 text）」时看的字段
_MEDIA_FIELDS = ("photo", "video", "document", "animation", "audio", "voice", "video_note", "sticker")


def _has_media(message) -> bool:
    """带媒体的消息（例如 /start 发的图片）正文是 caption，编辑要用 editMessageCaption。"""
    return any(getattr(message, field, None) for field in _MEDIA_FIELDS)


def _button_to_json(button):
    """pyrogram 的 InlineKeyboardButton → Bot API 的按钮 JSON。识别不了的返回 None（跳过）。"""
    text = getattr(button, "text", None)
    if not text:
        return None

    data = getattr(button, "callback_data", None)
    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", "replace")
        return {"text": text, "callback_data": data}

    url = getattr(button, "url", None)
    if url:
        return {"text": text, "url": url}

    user_id = getattr(button, "user_id", None)
    if user_id:
        return {"text": text, "url": f"tg://user?id={user_id}"}

    for field in ("switch_inline_query", "switch_inline_query_current_chat"):
        value = getattr(button, field, None)
        if value is not None:
            return {"text": text, field: value}

    web_app = getattr(button, "web_app", None)
    if web_app is not None:
        return {"text": text, "web_app": {"url": getattr(web_app, "url", "")}}

    login_url = getattr(button, "login_url", None)
    if login_url is not None:
        item = {"url": getattr(login_url, "url", "")}
        for field in ("forward_text", "bot_username", "request_write_access"):
            value = getattr(login_url, field, None)
            if value is not None:
                item[field] = value
        return {"text": text, "login_url": item}

    if getattr(button, "callback_game", None) is not None:
        return {"text": text, "callback_game": {}}

    LOGGER.warning(f"copy_text: 跳过无法识别的按钮 {text!r}")
    return None


def _markup_to_json(markup, copy_value=None, label=COPY_BUTTON_LABEL):
    """原键盘 → Bot API 的 reply_markup JSON，并把「一键复制」按钮插到首行。"""
    rows = []
    for row in (getattr(markup, "inline_keyboard", None) or []):
        items = [item for item in (_button_to_json(b) for b in row) if item]
        if items:
            rows.append(items)
    if copy_value:
        rows.insert(0, [{"text": label, "copy_text": {"text": copy_value[:COPY_TEXT_MAX]}}])
    return {"inline_keyboard": rows}


async def _markdown_to_entities(text: str):
    """用 pyrogram 自己的解析器把 markdown 拆成 (纯文本, Bot API entities)。

    实体偏移量是 UTF-16 单位，正是 Bot API 要求的格式；渲染结果与走 MTProto 时一致。
    """
    out = await Parser(None).parse(text, enums.ParseMode.MARKDOWN)
    entities = []
    for entity in out["entities"]:
        kind = _ENTITY_TYPES.get(type(entity).__name__) or _ENTITY_TYPES.get(getattr(entity, "type", None) or "")
        if not kind:
            LOGGER.warning(f"copy_text: 未映射的实体 {type(entity).__name__}，已跳过")
            continue
        item = {"type": kind, "offset": entity.offset, "length": entity.length}
        if kind == "text_link":
            item["url"] = getattr(entity, "url", None) or ""
        elif kind == "text_mention":
            user = getattr(entity, "user", None)
            if user is None:
                continue
            item["user"] = {
                "id": getattr(user, "id", 0),
                "is_bot": bool(getattr(user, "is_bot", False)),
                "first_name": getattr(user, "first_name", None) or "",
            }
        elif kind == "pre":
            language = getattr(entity, "language", None)
            if language:
                item["language"] = language
        elif kind == "custom_emoji":
            custom_emoji_id = getattr(entity, "custom_emoji_id", None)
            if not custom_emoji_id:
                continue
            item["custom_emoji_id"] = str(custom_emoji_id)
        entities.append(item)
    return out["message"], entities


async def _api_call(method: str, payload: dict):
    """调一次 HTTP Bot API，返回 (是否成功, 失败原因)。"""
    url = f"{API_ROOT}/bot{bot_token}/{method}"
    try:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if isinstance(data, dict) and data.get("ok"):
        return True, ""
    if isinstance(data, dict):
        return False, str(data.get("description") or data)
    return False, str(data)


async def editMessageWithCopy(message, text: str, buttons=None, copy_text=None,
                              label: str = COPY_BUTTON_LABEL, timer=None):
    """编辑消息，并在键盘首行插入「一键复制」按钮。

    :param message: CallbackQuery 或 Message（与 editMessage 一致）
    :param text: markdown 正文（与 editMessage 一致）
    :param buttons: 原键盘 InlineKeyboardMarkup
    :param copy_text: 要复制的文本（通常就是密码）；为空则不插入按钮，直接走 editMessage
    :return: True 成功 / False 未生效 / str 异常（与 editMessage 返回值语义一致）
    """
    if isinstance(message, CallbackQuery):
        message = message.message

    copy_value = "" if copy_text is None else str(copy_text).strip()
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "id", None)
    if not (bot_token and chat_id is not None and message_id is not None and copy_value):
        return await editMessage(message, text, buttons=buttons, timer=timer)

    try:
        parsed, entities = await _markdown_to_entities(text)
    except Exception as e:
        LOGGER.warning(f"copy_text: 解析 markdown 失败 {type(e).__name__}: {e}，回退 editMessage")
        return await editMessage(message, text, buttons=buttons, timer=timer)

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": _markup_to_json(buttons, copy_value, label),
    }
    if _has_media(message):
        method = "editMessageCaption"
        payload["caption"] = parsed
        payload["caption_entities"] = entities
    else:
        method = "editMessageText"
        payload["text"] = parsed
        payload["entities"] = entities
        payload["link_preview_options"] = {"is_disabled": True}

    ok, why = await _api_call(method, payload)
    if ok:
        LOGGER.info(f"copy_text 按钮已附加 method={method} chat={chat_id} msg={message_id}")
        if timer is not None:
            return await deleteMessage(message, timer)
        return True
    if "not modified" in why.lower():
        LOGGER.info(f"copy_text 未生效[消息无变化] chat={chat_id} msg={message_id}")
        return False
    LOGGER.warning(f"copy_text 编辑失败[{method}] chat={chat_id} msg={message_id}: {why}，回退 editMessage")
    return await editMessage(message, text, buttons=buttons, timer=timer)
