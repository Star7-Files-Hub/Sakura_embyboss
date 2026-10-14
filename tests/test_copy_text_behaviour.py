# -*- coding: utf-8 -*-
"""copy_text helper 的行为测试：执行真实模块，标准库桩掉外部依赖。"""
import asyncio
import importlib.util
import os
import sys
import types

REPO = os.environ.get("COPY_TEXT_TEST_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "bot/func_helper/copy_text.py")
PASS = FAIL = 0

def check(name, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {extra}")

def module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    sys.modules[name] = value
    return value

class ParseMode:
    MARKDOWN = "MARKDOWN"
    HTML = "HTML"
    DEFAULT = "DEFAULT"
    DISABLED = "DISABLED"

class CallbackQuery: pass
class MessageEntityBold:
    type = "bold"
    def __init__(self, offset=0, length=0): self.offset, self.length = offset, length
class MessageEntityCustomEmoji:
    type = "custom_emoji"
    def __init__(self, offset=0, length=1, document_id=123): self.offset, self.length, self.document_id = offset, length, document_id
class InputMessageEntityMentionName:
    type = "mention_name"
    def __init__(self, offset=0, length=4, user_id=42): self.offset, self.length, self.user_id = offset, length, user_id

class Parser:
    def __init__(self, client=None): pass
    async def parse(self, text, parse_mode):
        if parse_mode == ParseMode.DISABLED:
            return {"message": text, "entities": None}
        if text == "mention":
            return {"message": text, "entities": [InputMessageEntityMentionName()]}
        if text == "emoji":
            return {"message": text, "entities": [MessageEntityCustomEmoji()]}
        if parse_mode in (ParseMode.DEFAULT, ParseMode.MARKDOWN):
            cleaned = text.replace("**", "")
            entities = [MessageEntityBold(0, 4)] if "**" in text else None
            return {"message": cleaned, "entities": entities}
        return {"message": text, "entities": []}

class Button:
    def __init__(self, text, **kwargs):
        self.text = text
        for key, value in kwargs.items(): setattr(self, key, value)

class Markup:
    def __init__(self, rows): self.inline_keyboard = rows

class ClientTimeout:
    def __init__(self, total=None): self.total = total

CAPTURED = []
RESPONSE = {"ok": True}
FAIL_REQUEST = False
class Response:
    async def json(self, content_type=None): return dict(RESPONSE)
class Request:
    async def __aenter__(self): return Response()
    async def __aexit__(self, *args): return False
class FailingRequest:
    async def __aenter__(self): raise RuntimeError(f"proxy failure p%40ss token {BOT_TOKEN}")
    async def __aexit__(self, *args): return False
class ClientSession:
    def __init__(self, timeout=None): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    def post(self, url, **kwargs):
        CAPTURED.append((url, kwargs))
        return FailingRequest() if FAIL_REQUEST else Request()

class Logger:
    def __init__(self): self.lines=[]
    def warning(self, text): self.lines.append(text)
    def info(self, text): self.lines.append(text)
class Proxy:
    scheme=""; hostname=""; port=None; username=""; password=""
class Config: proxy=Proxy()
class Bot: parse_mode=ParseMode.MARKDOWN
LOGGER=Logger()
BOT_TOKEN="12345:SECRET"
CALLS=[]
async def edit_message(message, text, buttons=None, timer=None, parse_mode=None):
    CALLS.append((text, buttons, timer, parse_mode))
    return True
async def delete_message(message, timer=None): return True

pyrogram = module("pyrogram", enums=types.SimpleNamespace(ParseMode=ParseMode))
parser_module = module("pyrogram.parser", Parser=Parser)
types_module = module("pyrogram.types", CallbackQuery=CallbackQuery)
module("aiohttp", ClientSession=ClientSession, ClientTimeout=ClientTimeout)
module("bot", LOGGER=LOGGER, bot=Bot(), bot_token=BOT_TOKEN, config=Config())
module("bot.func_helper")
module("bot.func_helper.msg_utils", editMessage=edit_message, deleteMessage=delete_message)

spec = importlib.util.spec_from_file_location("copy_text_under_test", SRC)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

async def run():
    rows=[[Button("old", callback_data="cb")], []]
    markup=Markup(rows)
    value=ct._markup_to_json(markup, "PWD")
    check("copy button inserted before complete existing rows", value["inline_keyboard"] == [[{"text":"🔑 复制密码","copy_text":{"text":"PWD"}}],[{"text":"old","callback_data":"cb"}]])
    check("long copy value limited to Bot API max", len(ct._markup_to_json(markup, "x"*300)["inline_keyboard"][0][0]["copy_text"]["text"]) == 256)
    check("unknown buttons reject whole conversion", _raises(lambda: ct._markup_to_json(Markup([[Button("unknown")]]), "p")))
    check("None markup rejects instead of replacing existing keyboard", _raises(lambda: ct._markup_to_json(None,"p")))
    check("empty rows are omitted", [] not in value["inline_keyboard"])

    Config.proxy=types.SimpleNamespace(scheme="http",hostname="proxy",port=8080,username="u ser",password="p@ss")
    check("HTTP proxy authentication URL escaped", ct._proxy_url() == "http://u%20ser:p%40ss@proxy:8080")
    redacted=ct._redact("token 12345:SECRET proxy u%20ser:p%40ss@proxy")
    check("bot token and encoded proxy credentials redacted", "SECRET" not in redacted and "p%40ss" not in redacted and "u%20ser" not in redacted)

    text="**bold**"
    parsed, entities=await ct._markdown_to_entities(text, None)
    check("None parse mode resolves Client default and keeps entities", parsed=="bold" and len(entities)==1)
    parsed, entities=await ct._markdown_to_entities("plain", ParseMode.MARKDOWN)
    check("plain text with no entities does not fail", parsed=="plain" and entities==[])
    parsed, entities=await ct._markdown_to_entities("mention", ParseMode.MARKDOWN)
    check("text mention converts raw user_id", entities==[{"type":"text_mention","offset":0,"length":4,"user":{"id":42}}])
    parsed, entities=await ct._markdown_to_entities("emoji", ParseMode.MARKDOWN)
    check("custom emoji converts raw document_id", entities==[{"type":"custom_emoji","offset":0,"length":1,"custom_emoji_id":"123"}])
    parsed, entities=await ct._markdown_to_entities(text, ParseMode.DISABLED)
    check("DISABLED preserves plain text without entities", parsed==text and entities==[])

    disabled_text, disabled_entities=await ct._markdown_to_entities(text, ParseMode.DISABLED)
    check("DISABLED parser result is plain without entities", disabled_text==text and disabled_entities==[])

    message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10),id=20)
    markup=Markup([[Button("old",callback_data="cb")]])
    CAPTURED.clear(); CALLS.clear(); RESPONSE.clear(); RESPONSE["ok"]=True
    await ct.editMessageWithCopy(message,text,markup,"pwd",parse_mode=None)
    payload=CAPTURED[-1][1]["json"]
    check("None mode HTTP payload includes default-mode entities", payload.get("entities")==[{"type":"bold","offset":0,"length":4}])

    CAPTURED.clear(); CALLS.clear()
    await ct.editMessageWithCopy(message,"plain",markup,"pwd",parse_mode=ParseMode.DISABLED)
    payload=CAPTURED[-1][1]["json"]
    check("DISABLED HTTP payload omits entities", "entities" not in payload)

    Config.proxy=types.SimpleNamespace(scheme="http",hostname="proxy",port=8080,username="u ser",password="p@ss")
    CAPTURED.clear(); CALLS.clear(); RESPONSE.clear(); RESPONSE["ok"]=True
    await ct.editMessageWithCopy(message,text,markup,"pwd")
    check("HTTP proxy settings are passed to aiohttp", CAPTURED[-1][1].get("proxy")=="http://u%20ser:p%40ss@proxy:8080")

    Config.proxy=types.SimpleNamespace(scheme="socks5",hostname="proxy",port=1080,username="",password="")
    CAPTURED.clear(); CALLS.clear()
    await ct.editMessageWithCopy(message,text,markup,"pwd")
    check("SOCKS proxy never silently goes direct and falls back", len(CAPTURED)==0 and len(CALLS)==1)

    Config.proxy=types.SimpleNamespace(scheme="",hostname="",port=None,username="",password="")
    CAPTURED.clear(); CALLS.clear(); RESPONSE.clear(); RESPONSE.update(ok=False,description="Bad Request token 12345:SECRET")
    await ct.editMessageWithCopy(message,text,markup,"pwd")
    check("API error fallback redacts token", len(CALLS)==1 and "SECRET" not in LOGGER.lines[-1])

    global FAIL_REQUEST
    FAIL_REQUEST=True
    Config.proxy=types.SimpleNamespace(scheme="http",hostname="proxy",port=8080,username="u ser",password="p@ss")
    CAPTURED.clear(); CALLS.clear()
    await ct.editMessageWithCopy(message,text,markup,"pwd")
    check("request exception redacts token and encoded proxy password", len(CALLS)==1 and "SECRET" not in LOGGER.lines[-1] and "p%40ss" not in LOGGER.lines[-1])
    FAIL_REQUEST=False
    RESPONSE.clear(); RESPONSE["ok"]=True
    print(f"RESULT PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0

def _raises(fn):
    try: fn()
    except (ValueError, TypeError): return True
    return False

if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
