# -*- coding: utf-8 -*-
"""
miniapp.py — 会员自助中心（Telegram Mini App）

挂在现有 FastAPI（8838）上：
    GET  /app/                  -> 页面 HTML
    GET  /app/miniapp.js        -> 前端脚本
    GET  /app/miniapp.css       -> 前端样式
    GET  /app/api/profile       -> 当前用户数据（Telegram initData 验签）
    GET  /app/api/poster/{id}   -> 海报代理（服务端带 api_key，前端不接触密钥）

鉴权只用 Telegram WebApp initData（HMAC-SHA256 + bot_token），
不接受任何 tg 参数，避免越权查看他人数据。
"""
import asyncio
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from bot import bot_token, LOGGER, emby_url, sakura_b, config
from bot.func_helper.emby import emby
from bot.sql_helper.sql_emby2 import sql_update_emby2 as _acct_sql2
from bot.sql_helper.sql_partition import sql_redeem_partition_code_atomic as _acct_redeem
from bot.sql_helper.sql_emby import sql_get_emby

route = APIRouter(prefix="/app", tags=["会员中心MiniApp"])

_DIR = Path(__file__).parent
_html_cache = {"mtime": 0, "body": None}

# initData 有效期（秒）。页面每次打开都是新生成的，超时只影响旧链接重放。
INIT_DATA_MAX_AGE = 12 * 3600

LEVEL_TEXT = {"a": "白名单", "b": "正常", "c": "已禁用", "d": "未注册"}
LEVEL_KEY = {"a": "vip", "b": "ok", "c": "bad", "d": "new"}
ITEM_TYPE = {"Movie": "电影", "Episode": "剧集", "Audio": "音乐", "Series": "剧集"}


def _verify_init_data(init_data: str):
    """校验 Telegram WebApp initData，返回 user dict 或 None。"""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        # Bot API 8.0 起 initData 会附带 Ed25519 的 signature 字段，
        # 注意：Bot API 8.x 的 signature 字段是否参与 data-check-string 因客户端而异
        # （实测新版客户端会把 signature 一起算进 HMAC），所以两种都试。
        if not received:
            return None
        pairs_nosig = dict(pairs)
        pairs_nosig.pop("signature", None)
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        ok = False
        for cand in (pairs, pairs_nosig):
            check = "\n".join(f"{k}={cand[k]}" for k in sorted(cand))
            calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(calc, received):
                ok = True
                break
        if not ok:
            check = "\n".join(f"{k}={pairs_nosig[k]}" for k in sorted(pairs_nosig))
            LOGGER.warning("【MiniApp】initData 验签失败 keys=" + ",".join(sorted(pairs))
                           + " recv=" + str(received)[:20]
                           + " raw=" + str(init_data)[:900])
            LOGGER.warning("【MiniApp】check=" + repr(check[:600]))
            return None
        try:
            auth_date = int(pairs.get("auth_date", "0"))
        except ValueError:
            return None
        if INIT_DATA_MAX_AGE and time.time() - auth_date > INIT_DATA_MAX_AGE:
            return None
        raw_user = pairs.get("user")
        return json.loads(raw_user) if raw_user else None
    except Exception as exc:  # 任何异常都按未授权处理，不回显细节
        LOGGER.warning(f"【MiniApp】initData 校验异常: {type(exc).__name__}")
        return None


def _load_html() -> str:
    path = _DIR / "miniapp.html"
    try:
        stamp = path.stat().st_mtime
        if _html_cache["body"] is None or stamp != _html_cache["mtime"]:
            body = path.read_text(encoding="utf-8")
            body = (body.replace("__CURRENCY__", sakura_b or "奶茶")
                        .replace("__BOT_NAME__", "茶百道"))
            _html_cache["body"] = body
            _html_cache["mtime"] = stamp
        return _html_cache["body"]
    except Exception as exc:
        LOGGER.error(f"【MiniApp】读取页面失败: {exc}")
        return "<h1>页面加载失败</h1>"


def _static(name: str, mime: str):
    async def _handler():
        try:
            body = (_DIR / name).read_text(encoding="utf-8")
            if name == "miniapp.js":
                body = body.replace("__EMBY_URL__", emby_url or "")
            return Response(body, media_type=mime, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            LOGGER.error(f"【MiniApp】读取 {name} 失败: {exc}")
            return Response("", media_type=mime)

    return _handler


@route.api_route("", methods=["GET", "HEAD"], include_in_schema=False)
@route.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def miniapp_index():
    return HTMLResponse(_load_html(), headers={"Cache-Control": "no-store"})


@route.get("/miniapp.js", include_in_schema=False)
async def miniapp_js():
    return await _static("miniapp.js", "application/javascript")()


@route.get("/miniapp.css", include_in_schema=False)
async def miniapp_css():
    return await _static("miniapp.css", "text/css")()


_miniapp_img_patched = True

_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"}


_avatar_patched = True

_AVATAR_DIR = _DIR / "img" / "avatar"
_AVATAR_TTL = 7 * 86400
_AVATAR_MISS = {}


async def _tg_avatar(tg_id):
    """取 Telegram 头像：服务端拉一次，缓存到 img/avatar/<uid>.jpg，返回站内 URL。

    Telegram 的 WebAppUser.photo_url 只在「附件菜单」入口下发，
    从菜单按钮 / 内联按钮进来的 initData 里通常为空，所以这里直接走 Bot API。
    """
    try:
        uid = int(tg_id or 0)
    except Exception:
        return ""
    if uid <= 0:
        return ""
    try:
        _AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        f = _AVATAR_DIR / (str(uid) + ".jpg")
        if f.is_file() and (time.time() - f.stat().st_mtime) < _AVATAR_TTL:
            return "/app/img/avatar/%d.jpg?v=%d" % (uid, int(f.stat().st_mtime))
        miss = _AVATAR_MISS.get(uid)
        if miss and (time.time() - miss) < 3600:
            return ""
        api = "https://api.telegram.org/bot" + bot_token
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(api + "/getUserProfilePhotos",
                             params={"user_id": uid, "limit": 1}) as r:
                j = await r.json(content_type=None)
            photos = ((j or {}).get("result") or {}).get("photos") or []
            if not photos:
                _AVATAR_MISS[uid] = time.time()
                return ""
            file_id = photos[0][-1]["file_id"]
            async with s.get(api + "/getFile", params={"file_id": file_id}) as r2:
                j2 = await r2.json(content_type=None)
            fp = ((j2 or {}).get("result") or {}).get("file_path") or ""
            if not fp:
                _AVATAR_MISS[uid] = time.time()
                return ""
            async with s.get("https://api.telegram.org/file/bot" + bot_token + "/" + fp) as r3:
                data = await r3.read()
        if len(data) > 200:
            f.write_bytes(data)
            return "/app/img/avatar/%d.jpg?v=%d" % (uid, int(f.stat().st_mtime))
        _AVATAR_MISS[uid] = time.time()
    except Exception as exc:
        _AVATAR_MISS[uid] = time.time()
        LOGGER.warning("【MiniApp】头像拉取失败: %s" % exc)
    return ""


@route.get("/img/{name:path}", include_in_schema=False)
async def miniapp_img(name: str):
    """会员中心静态图（logo / 品牌 / 使用规范海报 / 等级图标）。"""
    from fastapi.responses import FileResponse
    root = (_DIR / "img").resolve()
    try:
        target = (root / name).resolve()
    except Exception:
        return Response(status_code=404)
    if root not in target.parents or target.suffix.lower() not in _IMG_EXT or not target.is_file():
        return Response(status_code=404)
    return FileResponse(str(target), headers={"Cache-Control": "public, max-age=86400"})


_live_cache = {"t": 0.0, "v": {"now": 0, "max": 0}}


def _age_s(val) -> float:
    """把 Emby 时间字符串转成距 now 的秒数；无法解析返回 None。"""
    try:
        txt = str(val or "").strip().replace("Z", "+00:00")
        txt = re.sub(r"\.\d+", "", txt)
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return time.time() - dt.timestamp()
    except Exception:
        return None

async def _count_playing() -> dict:
    """10 分钟活跃口径：online=近 10 分钟内有过活动的去重账号。"""
    if time.time() - _live_cache["t"] < 15:
        return _live_cache["v"]
    online, watching, playing = 0, 0, 0
    try:
        result = await emby._request("GET", "/emby/Sessions")
        sessions = result.data if getattr(result, "success", False) and result.data else []
        now = time.time()
        users = set()
        for _s in sessions or []:
            _uid = str(_s.get("UserId") or "")
            if _s.get("NowPlayingItem"):
                playing += 1
                if _uid:
                    watching += 1
            if not _uid:
                continue
            age = _age_s(_s.get("LastActivityDate"))
            if age is None or age <= 600:
                users.add(_uid)
        online = len(users)
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】统计在线人数失败: {exc}")
    _live_cache["t"] = time.time()
    _live_cache["v"] = {"now": playing, "max": 0, "online": online, "watching": watching}
    return _live_cache["v"]


_miniapp_watch_patched = True

_watch_cache = {}


async def _watch_hours(emby_id: str):
    """从 Emby 播放统计里取「近 30 天 / 累计」观看时长（小时，10 分钟缓存）。"""
    safe = str(emby_id or "")
    if not safe or not safe.replace("-", "").isalnum():
        return {"recent": None, "total": None}
    hit = _watch_cache.get(safe)
    if hit and time.time() - hit[0] < 600:
        return hit[1]

    async def _sum(where: str):
        sql = ("SELECT SUM(PlayDuration - PauseDuration) AS s FROM PlaybackActivity "
               "WHERE UserId = '" + safe + "'" + where)
        res = await emby._request(
            "POST", "/emby/user_usage_stats/submit_custom_query",
            json={"CustomQueryString": sql, "ReplaceUserId": False})
        if getattr(res, "success", False) and res.data and res.data.get("results"):
            row = res.data["results"][0]
            try:
                return round(float(row[0] or 0) / 3600.0, 1)
            except Exception:
                return None
        return None

    out = {"recent": None, "total": None}
    try:
        out["total"] = await _sum("")
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】累计观看时长读取失败: {exc}")
    try:
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 30 * 86400))
        out["recent"] = await _sum(" AND DateCreated >= '" + start + "'")
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】近30天观看时长读取失败: {exc}")
    _watch_cache[safe] = (time.time(), out)
    return out


_hot_cache = {}


@route.get("/api/hot", include_in_schema=False)
async def api_hot(request: Request, span: str = "today", limit: int = 30):
    """热播榜：按剧集/影片聚合播放次数（今日/昨日/近7天）"""
    init_data = (request.headers.get("X-Tg-Init-Data")
                 or request.query_params.get("initData") or "")
    if not _verify_init_data(init_data):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    span = span if span in ("today", "yest", "week") else "today"
    try:
        limit = max(1, min(int(limit), 50))
    except Exception:
        limit = 30
    key = "%s:%d" % (span, limit)
    now_ts = time.time()
    hit = _hot_cache.get(key)
    if hit and now_ts - hit[0] < 120:
        return JSONResponse(hit[1])

    from datetime import timedelta, timezone
    bj = timezone(timedelta(hours=8))
    now = datetime.now(bj)
    d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if span == "yest":
        start, end = d0 - timedelta(days=1), d0
    elif span == "week":
        start, end = d0 - timedelta(days=6), d0 + timedelta(days=1)
    else:
        start, end = d0, d0 + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    a = start.astimezone(timezone.utc).strftime(fmt)
    b = end.astimezone(timezone.utc).strftime(fmt)

    base = ("SELECT CASE WHEN instr(ItemName,' - s')>0 "
            "THEN substr(ItemName,1,instr(ItemName,' - s')-1) ELSE ItemName END AS T, "
            "COUNT(*) AS C FROM PlaybackActivity "
            "WHERE DateCreated >= '%s' AND DateCreated < '%s' "
            "GROUP BY T ORDER BY C DESC LIMIT %d" % (a, b, limit))
    items = []
    try:
        r = await emby._request("POST", "/emby/user_usage_stats/submit_custom_query",
                                json={"CustomQueryString": base, "ReplaceUserId": True})
        rows = (r.data or {}).get("results") if getattr(r, "success", False) else None
        for row in rows or []:
            if not row or len(row) < 2:
                continue
            name = str(row[0] or "").strip()
            if not name:
                continue
            items.append({"n": name, "c": int(row[1])})
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】热播榜查询失败: {exc}")
    payload = {"ok": True, "span": span, "items": items}
    _hot_cache[key] = (now_ts, payload)
    return JSONResponse(payload)


_hotdet_cache = {}
_hotimg_cache = {}


@route.get("/api/hot/detail", include_in_schema=False)
async def api_hot_detail(request: Request, n: str = ""):
    """热播榜点开：按剧名在 Emby 里查条目，返回海报/简介/年份。"""
    init_data = (request.headers.get("X-Tg-Init-Data")
                 or request.query_params.get("initData") or "")
    if not _verify_init_data(init_data):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    name = (n or "").strip()[:80]
    if not name:
        return JSONResponse({"ok": False, "error": "empty"})
    hit = _hotdet_cache.get(name)
    if hit and time.time() - hit[0] < 600:
        return JSONResponse(hit[1])
    payload = {"ok": False}
    try:
        from urllib.parse import quote as _q
        ep = ("/emby/Items?Recursive=true&Limit=8&IncludeItemTypes=Series,Movie"
              "&Fields=Overview,ProductionYear,ImageTags,SeriesStudio&SearchTerm=" + _q(name))
        r = await emby._request("GET", ep)
        rows = (r.data or {}).get("Items") if getattr(r, "success", False) else None
        rows = rows or []
        pick = None
        for row in rows:
            if str(row.get("Name") or "").strip() == name:
                pick = row
                break
        if pick is None and rows:
            pick = rows[0]
        if pick:
            tags = pick.get("ImageTags") or {}
            tag = str(tags.get("Primary") or "")
            ov = str(pick.get("Overview") or "").strip()
            if len(ov) > 260:
                ov = ov[:260].rstrip() + "…"
            payload = {
                "ok": True,
                "id": pick.get("Id") or "",
                "name": pick.get("Name") or name,
                "type": pick.get("Type") or "",
                "year": pick.get("ProductionYear") or 0,
                "ov": ov,
                "poster": ("/app/api/hotimg?id=%s&tag=%s" % (pick.get("Id") or "", tag)) if tag else "",
            }
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】热播详情失败 {name}: {exc}")
    _hotdet_cache[name] = (time.time(), payload)
    if len(_hotdet_cache) > 300:
        _hotdet_cache.clear()
    return JSONResponse(payload)


@route.get("/api/hotimg", include_in_schema=False)
async def api_hotimg(request: Request, id: str = "", tag: str = "", h: int = 420):
    """海报代理：<img> 带不了自定义头，用随机 id 白名单过滤，只透传 Emby 图片。"""
    iid = (id or "").strip()
    if not re.match(r"^[0-9a-zA-Z]{4,40}$", iid):
        return Response(status_code=204)
    try:
        h = max(120, min(int(h), 800))
    except Exception:
        h = 420
    ck = "%s:%d" % (iid, h)
    if ck in _hotimg_cache:
        return Response(content=_hotimg_cache[ck], media_type="image/jpeg",
                        headers={"Cache-Control": "public,max-age=86400"})
    q = "/emby/Items/%s/Images/Primary?maxHeight=%d" % (iid, h)
    if tag and re.match(r"^[0-9a-zA-Z_]{4,60}$", tag):
        q += "&tag=" + tag
    try:
        r = await emby._request("GET", q)
        if getattr(r, "success", False) and isinstance(r.data, (bytes, bytearray)) and r.data:
            if len(_hotimg_cache) > 200:
                _hotimg_cache.clear()
            _hotimg_cache[ck] = bytes(r.data)
            return Response(content=bytes(r.data), media_type="image/jpeg",
                            headers={"Cache-Control": "public,max-age=86400"})
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】海报代理失败 {iid}: {exc}")
    return Response(status_code=204)


@route.get("/api/live", include_in_schema=False)
async def miniapp_live(request: Request):
    init_data = (request.headers.get("X-Tg-Init-Data")
                 or request.query_params.get("initData") or "")
    if not _verify_init_data(init_data):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    v = await _count_playing()
    return JSONResponse({"ok": True, "now": v["now"], "max": v["max"],
                         "online": v.get("online", 0), "watching": v.get("watching", v["now"])})


@route.get("/telegram-web-app.js", include_in_schema=False)
async def miniapp_twa():
    # 本地缓存一份官方 SDK：国内直连 telegram.org 经常失败，页面会白屏
    return await _static("telegram-web-app.js", "application/javascript")()


def _norm_id(value) -> str:
    return (value or "").replace("-", "").lower()


def _fmt_dt(value, fmt: str = "%Y-%m-%d") -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime(fmt)
    return str(value)[:19]


async def _load_sessions(embyid: str):
    """当前会话 + 正在播放。"""
    playing, online = [], 0
    try:
        result = await emby._request("GET", "/emby/Sessions")
        sessions = result.data if getattr(result, "success", False) and result.data else []
        mine = _norm_id(embyid)
        for s in sessions or []:
            if _norm_id(s.get("UserId")) != mine:
                continue
            online += 1
            item = s.get("NowPlayingItem") or {}
            if not item:
                continue
            kind = item.get("Type", "")
            if kind == "Episode":
                title = item.get("SeriesName") or item.get("Name", "未知")
                sub = item.get("Name", "")
            else:
                title = item.get("Name", "未知")
                sub = ""
            pos = (s.get("PlayState") or {}).get("PositionTicks") or 0
            dur = item.get("RunTimeTicks") or 0
            playing.append({
                "id": item.get("Id", ""),
                "title": title,
                "sub": sub,
                "type": ITEM_TYPE.get(kind, kind),
                "device": s.get("DeviceName") or "未知设备",
                "client": s.get("Client") or "",
                "percent": min(100, round(pos / dur * 100)) if dur else 0,
                "pos": _ticks_text(pos),
                "dur": _ticks_text(dur),
            })
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】读取会话失败: {exc}")
    return playing, online


def _ticks_text(ticks: int) -> str:
    seconds = int((ticks or 0) // 10_000_000)
    if seconds <= 0:
        return ""
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}:{m:02d}" if h else f"{m} 分"


async def _load_recent(embyid: str):
    """最近播放（用于海报列表）。"""
    out = []
    try:
        endpoint = ("/emby/Users/%s/Items?SortBy=DatePlayed&SortOrder=Descending&Recursive=true"
                    "&IncludeItemTypes=Movie,Episode&Filters=IsPlayed&Limit=9"
                    "&Fields=ProductionYear,SeriesName") % embyid
        result = await emby._request("GET", endpoint)
        items = (result.data or {}).get("Items", []) if getattr(result, "success", False) else []
        for it in items or []:
            is_ep = it.get("Type") == "Episode"
            out.append({
                "id": it.get("Id", ""),
                "title": (it.get("SeriesName") if is_ep else it.get("Name")) or "未知",
                "sub": it.get("Name", "") if is_ep else str(it.get("ProductionYear") or ""),
                "played": _fmt_dt(((it.get("UserData") or {}).get("LastPlayedDate")) or "")[:10],
            })
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】读取最近播放失败: {exc}")
    if not out:
        out = await _recent_from_activity(embyid)
    return out

async def _recent_from_activity(embyid: str):
    """兜底：直接从播放活动表取最近 6 条（未看完的也算）。"""
    out = []
    try:
        q = ("SELECT ItemName, DateCreated, PlayDuration FROM PlaybackActivity "
             "WHERE UserId='%s' ORDER BY DateCreated DESC LIMIT 9" % (embyid or ""))
        res = await emby._request("POST", "/emby/user_usage_stats/submit_custom_query",
                                  json={"CustomQueryString": q, "ReplaceUserId": False})
        data = (res.data or {}) if getattr(res, "success", False) else {}
        cols = [str(c).lower() for c in (data.get("colums") or data.get("columns") or [])]
        rows = data.get("results") or []
        try:
            i_n = cols.index("itemname")
            i_d = cols.index("datecreated")
        except ValueError:
            i_n, i_d = 0, 1
        for row in rows:
            if not row:
                continue
            name = str(row[i_n] or "").strip()
            if not name:
                continue
            out.append({"id": "", "title": name, "sub": "", "played": _fmt_dt(str(row[i_d] or ""))[:10]})
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】读取播放活动失败: {exc}")
    return out


async def _load_devices(embyid: str):
    devices, seen = [], set()
    try:
        ok, rows = await emby.get_emby_userip(emby_id=embyid)
        if ok and isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    device = row.get("DeviceName") or row.get("device") or ""
                    client = row.get("ClientName") or row.get("client") or ""
                    ip = row.get("RemoteAddress") or row.get("ip") or ""
                else:
                    device, client, ip = (list(row) + ["", "", ""])[:3]
                key = f"{device}|{client}|{ip}"
                if key in seen:
                    continue
                seen.add(key)
                devices.append({"device": device or "未知", "client": client or "", "ip": ip or ""})
                if len(devices) >= 10:
                    break
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】读取设备失败: {exc}")
    return devices


def _mm_lvov(tg):
    """等级覆盖：miniapp_kv 里 lvov:<tg> = 1..7，0/缺失表示按观影时长自动算"""
    try:
        rows = _mm_rows("SELECT v FROM miniapp_kv WHERE k=:k", k="lvov:%d" % int(tg))
        if not rows:
            return 0
        v = int(str(rows[0]["v"]).strip())
        return v if 1 <= v <= 7 else 0
    except Exception:
        return 0


@route.get("/api/profile", include_in_schema=False)
async def miniapp_profile(request: Request):
    init_data = (request.headers.get("X-Tg-Init-Data")
                 or request.query_params.get("initData") or "")
    tg_user = _verify_init_data(init_data)
    if not tg_user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    user = sql_get_emby(tg=tg_user.get("id"))
    if not user:
        LOGGER.warning("【MiniApp】账号未匹配 tg=" + str(tg_user.get("id"))
                       + " username=" + str(tg_user.get("username")))
        return JSONResponse({"ok": False, "error": "not_registered",
                             "tg": tg_user.get("id"),
                             "username": tg_user.get("username") or "",
                             "message": "还没有你的账号，请先在机器人里 /start 注册"},
                            status_code=200)

    now = datetime.now()
    ex, cr = user.ex, user.cr
    days_left, progress = None, 0
    if ex:
        days_left = (ex - now).days if isinstance(ex, datetime) else None
        if isinstance(cr, datetime) and ex > cr:
            total = (ex - cr).total_seconds()
            progress = max(0, min(100, round((now - cr).total_seconds() / total * 100)))

    playing, online, recent, devices = [], 0, [], []
    watch = {"recent": None, "total": None}
    try:
        (playing, online), recent, devices, watch = await asyncio.gather(
            _load_sessions(user.embyid or ""),
            _load_recent(user.embyid or ""),
            _load_devices(user.embyid or ""),
            _watch_hours(user.embyid or ""),
        )
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】并行读取失败: {exc}")

    admins = []
    try:
        admins = [int(x) for x in (getattr(config, "admins", None) or [])]
    except Exception:
        admins = []
    try:
        is_admin = int(tg_user.get("id") or 0) in admins
    except Exception:
        is_admin = False
    live = await _count_playing()

    name = (tg_user.get("first_name") or "") + (
        (" " + tg_user["last_name"]) if tg_user.get("last_name") else "")
    return JSONResponse({
        "ok": True,
        "me": {
            "name": user.name or "",
            "embyid": user.embyid or "",
            "level": user.lv or "",
            "level_text": LEVEL_TEXT.get(user.lv, "未知"),
            "level_key": LEVEL_KEY.get(user.lv, "new"),
            "created": _fmt_dt(cr),
            "expire": _fmt_dt(ex),
            "days_left": days_left,
            "progress": progress,
            "code_changed": _code_once_used(user.tg),
            "credit": user.iv,
            "emby_pwd": (getattr(user, "pwd", "") or ""),
            "tg_name": name.strip(),
            "username": tg_user.get("username") or "",
            "photo": (await _tg_avatar(tg_user.get("id"))) or (tg_user.get("photo_url") or ""),
            "currency": sakura_b or "奶茶",
        "ctitle": _mm_title(user.tg),
        },
        "playing": playing,
        "online": online,
        "recent": recent,
        "devices": devices,
        "server_time": now.strftime("%H:%M"),
        "admin": is_admin,
        "live": live,
        "watch30": watch.get("recent"),
        "watch_total": watch.get("total"),
        "lv_override": _mm_lvov(user.tg),
    })


_POSTER_ID = re.compile(r"^[A-Za-z0-9]{4,40}$")


@route.get("/api/poster/{item_id}", include_in_schema=False)
async def miniapp_poster(item_id: str, h: int = 200):
    """海报代理：前端拿不到 Emby 密钥，也避免跨域。"""
    if not _POSTER_ID.match(item_id or ""):
        return Response(status_code=404)
    height = max(80, min(int(h or 200), 400))
    url = f"{(emby_url or '').rstrip('/')}/emby/Items/{item_id}/Images/Primary"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            async with sess.get(url, params={"maxHeight": height, "quality": 82},
                                headers={"X-Emby-Token": emby.api_key}) as resp:
                if resp.status != 200:
                    return Response(status_code=404)
                data = await resp.read()
                return Response(data, media_type=resp.headers.get("Content-Type", "image/jpeg"),
                                headers={"Cache-Control": "public, max-age=86400"})
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】海报代理失败: {type(exc).__name__}")
        return Response(status_code=404)

# ===================== 签到（对齐 EmbyBoss）=====================

import random as _ck_rand
from datetime import datetime as _ck_dt, timezone as _ck_tz, timedelta as _ck_td
from sqlalchemy import text as _ck_text
from bot import _open as _ck_open
from bot.sql_helper.sql_emby import (
    Session as _ck_Session, sql_update_emby as _ck_update, Emby as _ck_Emby,
)

_CK_TZ = _ck_tz(_ck_td(hours=8))
_CK_DDL = (
    "CREATE TABLE IF NOT EXISTS emby_checkin ("
    "tg BIGINT NOT NULL, day DATE NOT NULL, pts INT NOT NULL DEFAULT 0, "
    "ts DATETIME NOT NULL, PRIMARY KEY (tg, day)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)
_ck_ready = [False]


def _ck_ensure():
    if _ck_ready[0]:
        return
    try:
        with _ck_Session() as s:
            s.execute(_ck_text(_CK_DDL))
            s.commit()
        _ck_ready[0] = True
    except Exception as e:
        LOGGER.warning("【MiniApp】签到表初始化失败 " + str(e))


def _ck_days(tg, days=45):
    _ck_ensure()
    start = (_ck_dt.now(_CK_TZ) - _ck_td(days=days)).strftime("%Y-%m-%d")
    try:
        with _ck_Session() as s:
            rows = s.execute(
                _ck_text("SELECT day, pts FROM emby_checkin WHERE tg=:t AND day>=:d"),
                {"t": tg, "d": start},
            ).fetchall()
        return {str(r[0])[:10]: int(r[1] or 0) for r in rows}
    except Exception as e:
        LOGGER.warning("【MiniApp】签到查询失败 " + str(e))
        return {}


def _ck_log(tg, day, pts):
    _ck_ensure()
    try:
        with _ck_Session() as s:
            s.execute(
                _ck_text(
                    "INSERT INTO emby_checkin (tg, day, pts, ts) VALUES (:t,:d,:p,:s) "
                    "ON DUPLICATE KEY UPDATE pts=:p"
                ),
                {"t": tg, "d": day, "p": int(pts),
                 "s": _ck_dt.now(_CK_TZ).strftime("%Y-%m-%d %H:%M:%S")},
            )
            s.commit()
    except Exception as e:
        LOGGER.warning("【MiniApp】签到写入失败 " + str(e))


def _ck_streak(days, signed_today):
    d = _ck_dt.now(_CK_TZ).date()
    if not signed_today:
        d = d - _ck_td(days=1)
    n = 0
    while n < 400 and str(d) in days:
        n += 1
        d = d - _ck_td(days=1)
    return n


def _ck_reward():
    """与 bot 完全一致：有 checkin_rewards 走加权随机，否则 checkin_reward 区间随机。"""
    try:
        rewards = _ck_open.checkin_rewards
        if rewards:
            return int(_ck_rand.choices(
                [int(i[0]) for i in rewards],
                weights=[float(i[1]) for i in rewards], k=1)[0])
        lo, hi = _ck_open.checkin_reward
        return _ck_rand.randint(int(lo), int(hi))
    except Exception:
        return _ck_rand.randint(1, 5)


def _ck_state(e):
    today = _ck_dt.now(_CK_TZ).strftime("%Y-%m-%d")
    days = _ck_days(getattr(e, "tg", 0))
    ch = getattr(e, "ch", None)
    chd = ch.strftime("%Y-%m-%d") if ch else ""
    signed = bool(chd) and chd >= today
    # 最近一次签到日若在明细表里缺行（老数据、写库失败），补进去：
    # 否则日历漏标、连续天数被截断成 1
    if chd and chd not in days:
        days[chd] = 0
    return {
        "signed": signed,
        "today": today,
        "credit": int(getattr(e, "iv", 0) or 0),
        "today_pts": int(days.get(today, 0)) if signed else 0,
        "streak": _ck_streak(days, signed),
        "days": days,
        "unit": sakura_b,
    }


@route.get("/api/checkin", include_in_schema=False)
async def miniapp_checkin_state(request: Request):
    tg_user = _verify_init_data(request.headers.get("X-Tg-Init-Data", ""))
    if not tg_user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not _ck_open.checkin:
        return JSONResponse({"ok": False, "error": "closed", "on": False})
    e = sql_get_emby(tg=tg_user.get("id"))
    if not e:
        return JSONResponse({"ok": False, "error": "not_registered",
                             "tg": tg_user.get("id")})
    st = _ck_state(e)
    st.update({"ok": True, "on": True})
    return JSONResponse(st)


@route.post("/api/checkin", include_in_schema=False)
async def miniapp_checkin(request: Request):
    tg_user = _verify_init_data(request.headers.get("X-Tg-Init-Data", ""))
    if not tg_user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not _ck_open.checkin:
        return JSONResponse({"ok": False, "error": "closed", "on": False})
    e = sql_get_emby(tg=tg_user.get("id"))
    if not e:
        return JSONResponse({"ok": False, "error": "not_registered",
                             "tg": tg_user.get("id")})

    now = _ck_dt.now(_CK_TZ)
    today = now.strftime("%Y-%m-%d")
    if e.ch and e.ch.strftime("%Y-%m-%d") >= today:
        st = _ck_state(e)
        st.update({"ok": False, "error": "signed"})
        return JSONResponse(st)

    cap = getattr(_ck_open, "checkin_lv", None)
    if cap and str(getattr(e, "lv", "")) > str(cap):
        return JSONResponse({"ok": False, "error": "forbidden"})

    reward = _ck_reward()
    _ck_update(_ck_Emby.tg == e.tg, iv=int(getattr(e, "iv", 0) or 0) + reward, ch=now)
    _ck_log(e.tg, today, reward)

    e2 = sql_get_emby(tg=e.tg) or e
    st = _ck_state(e2)
    st.update({"ok": True, "reward": reward})
    return JSONResponse(st)


# ================= 排行榜（真实数据） =================
import aiohttp as _rk_aio
import asyncio as _rk_async

_RK_MEM = {}
_RK_TGNAME = {}
_RK_TH = [1, 4, 10, 25, 50, 100, 200]


def _rk_lv(hours):
    lv = 1
    for i, h in enumerate(_RK_TH):
        if hours >= h:
            lv = i + 1
        else:
            break
    return lv


async def _rk_tg_card(tg):
    now = time.time()
    hit = _RK_TGNAME.get(int(tg))
    if hit and now - hit[0] < 21600:
        return hit[1]
    name = ""
    try:
        url = "https://api.telegram.org/bot" + str(bot_token) + "/getChat"
        async with _rk_aio.ClientSession(timeout=_rk_aio.ClientTimeout(total=5)) as s:
            async with s.get(url, params={"chat_id": int(tg)}) as r:
                j = await r.json()
        if j.get("ok"):
            u = j.get("result") or {}
            name = " ".join([x for x in [u.get("first_name"), u.get("last_name")] if x]).strip()
            if not name:
                name = u.get("title") or u.get("username") or ""
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】TG资料读取失败 {tg}: {exc}")
    _RK_TGNAME[int(tg)] = (now, name)
    return name


async def _rk_watch(where=""):
    sql = ("SELECT UserId, SUM(PlayDuration - PauseDuration) AS s FROM PlaybackActivity")
    if where:
        sql += " WHERE " + where
    sql += " GROUP BY UserId"
    out = {}
    try:
        res = await emby._request(
            "POST", "/emby/user_usage_stats/submit_custom_query",
            json={"CustomQueryString": sql, "ReplaceUserId": False})
        if getattr(res, "success", False) and res.data:
            for row in (res.data.get("results") or []):
                try:
                    out[str(row[0]).strip().lower()] = float(row[1] or 0)
                except Exception:
                    pass
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】排行榜统计失败: {exc}")
    return out


def _rk_span_where(span):
    now = time.time()
    lt = time.localtime(now)
    mid = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    if span == "yesterday":
        a = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mid - 86400))
        b = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mid))
        return "DateCreated >= '" + a + "' AND DateCreated < '" + b + "'"
    if span == "week":
        a = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mid - 6 * 86400))
        return "DateCreated >= '" + a + "'"
    a = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mid))
    return "DateCreated >= '" + a + "'"


def _rk_lvov_all():
    """等级人工覆盖表：miniapp_kv 里 lvov:<tg> = 1..7"""
    out = {}
    try:
        rows = _mm_rows("SELECT k, v FROM miniapp_kv WHERE k LIKE 'lvov:%'")
        for r in rows:
            try:
                tg = int(str(r["k"]).split(":", 1)[1])
                n = int(str(r["v"]).strip())
            except Exception:
                continue
            if 1 <= n <= 7:
                out[tg] = n
    except Exception:
        pass
    return out


@route.get("/api/rank", include_in_schema=False)
async def miniapp_rank(request: Request):
    init_data = (request.headers.get("X-Tg-Init-Data")
                 or request.query_params.get("initData") or "")
    tg_user = _verify_init_data(init_data)
    if not tg_user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    board = (request.query_params.get("board") or "watch").strip().lower()
    span = (request.query_params.get("span") or "today").strip().lower()
    if board not in ("watch", "point"):
        board = "watch"
    _SPAN_ALIAS = {"yest": "yesterday", "y": "yesterday", "d1": "yesterday",
                   "7d": "week", "weekly": "week", "all": "week"}
    span = _SPAN_ALIAS.get(span, span)
    if span not in ("today", "yesterday", "week"):
        span = "today"
    cache_key = board + "|" + (span if board == "watch" else "all")
    _my_tg = int(tg_user.get("id") or 0)
    hit = _RK_MEM.get(cache_key)
    if hit and time.time() - hit[0] < 120 and len(hit) >= 4:
        # 缓存里绝不能带别人的 me：命中后按当前用户单独算名次
        _pl = dict(hit[1])
        _pl["me"] = _rk_me(hit, _my_tg)
        return JSONResponse(_pl)

    rows = []
    try:
        with _ck_Session() as s:
            rows = s.execute(_ck_text(
                "SELECT tg, embyid, name, lv, iv, ex FROM emby")).fetchall()
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】排行榜读库失败: {exc}")

    by_emby = {}
    for r in rows:
        by_emby[str(r[1] or "").strip().lower()] = r

    totals = await _rk_watch("")

    entries = []
    if board == "point":
        for r in rows:
            entries.append([int(r[0]), int(r[4] or 0), r[2] or "", str(r[1] or "").lower()])
        entries.sort(key=lambda x: (-x[1], -x[0]))
    else:
        vals = await _rk_watch(_rk_span_where(span))
        for uid, sec in vals.items():
            r = by_emby.get(uid)
            if not r:
                continue
            entries.append([int(r[0]), int(round(sec / 60.0)), r[2] or "", uid])
        entries.sort(key=lambda x: (-x[1], -x[0]))

    my_tg = int(tg_user.get("id") or 0)
    me = None
    _mine = next((e for e in entries if e[0] == my_tg), None)
    if _mine is not None:
        # 竞赛式排名：比我高的有几个，我就是第几名（同分并列，不再看数组下标）
        _better = sum(1 for e in entries if e[1] > _mine[1])
        _ties = sum(1 for e in entries if e[1] == _mine[1])
        me = {"rk": _better + 1, "v": _mine[1], "ties": _ties}

    top = entries[:30]
    names = {}
    try:
        sem = _rk_async.Semaphore(8)

        async def _one(tg):
            async with sem:
                names[tg] = await _rk_tg_card(tg)

        await _rk_async.gather(*[_one(e[0]) for e in top])
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】TG资料批量读取失败: {exc}")

    # 头像：不在榜单请求里阻塞拉取（50 个 × 8s 会把接口拖死），
    # 只回懒加载地址，由 <img> 触发 /app/api/avatar 按需拉取并落盘
    _ovmap = _rk_lvov_all()
    _tmap = _mm_titles_all()
    items = []
    for e in top:
        tg, v, emby_name, uid = e
        nm = names.get(tg) or emby_name or ("用户" + str(tg)[-4:])
        hours = round(totals.get(uid, 0) / 3600.0, 1)
        items.append({
            "tg": tg,
            "n": nm,
            "av": _rk_av_url(tg),
            "lv": (_ovmap.get(tg) or _rk_lv(hours)),
            "t": _tmap.get(tg) or "",
            "v": v,
        })

    payload = {"ok": True, "board": board, "span": span,
               "unit": "分钟" if board == "watch" else (sakura_b or "奶茶"),
               "items": items, "me": me}
    _vmap = {e[0]: e[1] for e in entries}
    _vals = sorted(_vmap.values(), reverse=True)
    _RK_MEM[cache_key] = (time.time(), payload, _vmap, _vals)
    return JSONResponse(payload)




def _rk_me(hit, my_tg):
    """按缓存的 (vmap, 排序值) 给当前用户算竞赛式名次，避免串号。"""
    try:
        vmap, vals = hit[2], hit[3]
    except Exception:
        return None
    v = vmap.get(my_tg)
    if v is None:
        return None
    better = 0
    for x in vals:
        if x > v:
            better += 1
        else:
            break
    return {"rk": better + 1, "v": v, "ties": sum(1 for x in vals if x == v)}


def _rk_av_url(tg):
    """有磁盘缓存直接给站内地址；没有则给懒加载接口。"""
    try:
        uid = int(tg)
        f = _AVATAR_DIR / (str(uid) + ".jpg")
        if f.is_file():
            return "/app/img/avatar/%d.jpg?v=%d" % (uid, int(f.stat().st_mtime))
        return "/app/api/avatar?tg=%d" % uid
    except Exception:
        return ""


@route.get("/api/avatar", include_in_schema=False)
async def miniapp_avatar(request: Request, tg: int = 0):
    """头像懒加载：首次从 Telegram 拉取并落盘，之后直接读文件。"""
    if tg <= 0:
        return Response(status_code=204)
    url = ""
    try:
        url = await _tg_avatar(tg)
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】头像获取失败 {tg}: {exc}")
    if not url:
        # 拉不到（没设头像 / 没跟机器人聊过）→ 204，前端自动退回首字母
        return Response(status_code=204, headers={"Cache-Control": "public,max-age=600"})
    try:
        f = _AVATAR_DIR / (str(int(tg)) + ".jpg")
        return FileResponse(str(f), media_type="image/jpeg",
                            headers={"Cache-Control": "public,max-age=86400"})
    except Exception:
        return Response(status_code=204)

# ================= MINIAPP_ACCT_V1 真实账户操作 / 设备 / 兑换 =================
import secrets as _acct_secrets
from datetime import datetime as _acct_dt, timedelta as _acct_td


def _acct_user(request):
    tg = _verify_init_data(request.headers.get("X-Tg-Init-Data", ""))
    if not tg:
        return None, None
    e = sql_get_emby(tg)
    return tg, e


# ================= MINIAPP_ACCT_V1 账户/设备/兑换 真实接口 =================
import re as _re_acct
import secrets as _acct_secrets
from datetime import datetime as _acct_dt, timedelta as _acct_td

try:
    from bot.sql_helper.sql_emby2 import sql_get_emby2, sql_update_emby2
except Exception:  # pragma: no cover
    sql_get_emby2 = sql_update_emby2 = None

try:
    from bot.sql_helper.sql_code import sql_get_code, sql_update_code
except Exception:  # pragma: no cover
    sql_get_code = sql_update_code = None

try:
    from bot.sql_helper.sql_emby import sql_update_emby, sql_delete_emby, Emby
except Exception:  # pragma: no cover
    pass

try:
    from bot import LOGGER
except Exception:  # pragma: no cover
    import logging
    LOGGER = logging.getLogger("miniapp")


def _acct_ctx(request):
    u = _verify_init_data(request.headers.get("X-Tg-Init-Data", ""))
    if not u:
        return None, None
    e = sql_get_emby(tg=u.get("id"))
    if not e:
        e = sql_get_emby(u.get("id"))
    if not e:
        return u, None
    return u, e


def _acct_err(msg, **kw):
    d = {"ok": False, "error": msg}
    d.update(kw)
    return JSONResponse(d)


_ACCT_PWD2_ONCE_READY = [False]
_ACCT_PWD2_ONCE_DDL = ("CREATE TABLE IF NOT EXISTS emby_pwd2_once ("
                       "tg BIGINT PRIMARY KEY, used_at DATETIME NOT NULL"
                       ") DEFAULT CHARSET=utf8mb4")


def _code_once_used(tg):
    con = None
    try:
        con = _ck_Session()
        if not _ACCT_PWD2_ONCE_READY[0]:
            con.execute(_ck_text(_ACCT_PWD2_ONCE_DDL))
            con.commit()
            _ACCT_PWD2_ONCE_READY[0] = True
        row = con.execute(_ck_text("SELECT 1 FROM emby_pwd2_once WHERE tg=:tg LIMIT 1"),
                          {"tg": int(tg)}).fetchone()
        return row is not None
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】读取安全码修改记录失败: {ex}")
        return False
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def _code_mark_used(tg):
    con = None
    try:
        con = _ck_Session()
        con.execute(_ck_text(_ACCT_PWD2_ONCE_DDL))
        con.execute(_ck_text("INSERT IGNORE INTO emby_pwd2_once (tg, used_at) VALUES (:tg, NOW())"),
                    {"tg": int(tg)})
        con.commit()
        return True
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】写入安全码修改记录失败: {ex}")
        return False
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def _acct_cur_code(e):
    # 安全码就存在 emby.pwd2（ORM 透明解密），不要再去查 emby2 表——
    # 之前查错表导致「没设过安全码」的账号可以直接删除账号（2026-10-26 事故）
    return (getattr(e, "pwd2", "") or "").strip()


def _acct_check_code(e, val):
    cur = _acct_cur_code(e)
    if not cur:
        return _acct_err("no_code", hint="请先设置安全码，再进行此操作")
    if val != cur:
        return _acct_err("bad_pwd2", hint="安全码不正确")
    return None


@route.post("/api/account", include_in_schema=False)
async def miniapp_account(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    try:
        body = await request.json()
    except Exception:
        body = {}
    act = str(body.get("act") or "")
    val = str(body.get("value") or "").strip()

    if act == "reset":
        _bad = _acct_check_code(e, val)
        if _bad is not None:
            return _bad
        pwd = _acct_secrets.token_urlsafe(9)[:10]
        ok = await emby.emby_reset(e.embyid, pwd)
        if not ok:
            return _acct_err("reset_failed")
        return JSONResponse({"ok": True, "pwd": pwd})

    if act == "code":
        if (not val) or len(val) > 32 or any(ch.isspace() for ch in val):
            return _acct_err("bad_code", hint="安全码不能为空、不能含空格，最多 32 个字符")
        cur = _acct_cur_code(e)
        if cur and cur == val:
            return _acct_err("same_code", hint="新安全码与当前相同")
        if _code_once_used(e.tg):
            return _acct_err("already_set", hint="修改安全码的机会已用完，如需重置请联系管理员")
        ok = _ck_update(_ck_Emby.tg == e.tg, pwd2=val)
        if not ok:
            try:
                ok = bool(sql_update_emby2 and sql_update_emby2(e.name, pwd2=val))
            except Exception:
                ok = False
        if not ok:
            return _acct_err("save_failed")
        _code_mark_used(e.tg)
        return JSONResponse({"ok": True, "message": "安全码已更新"})

    if act == "rebind":
        if not _re_acct.match(r"^\d{5,15}$", val):
            return _acct_err("bad_tg", hint="请输入新的 Telegram 数字 ID")
        newtg = int(val)
        if newtg == int(e.tg):
            return _acct_err("same_tg", hint="与当前绑定相同")
        if sql_get_emby(newtg) and int(newtg) != int(e.tg):
            return _acct_err("tg_taken", hint="该 Telegram 已绑定其他账号")
        ok = _ck_update(_ck_Emby.tg == int(e.tg), tg=newtg)
        return JSONResponse({"ok": True, "tg": newtg}) if ok else _acct_err("save_failed")

    if act == "delete":
        if not val:
            return _acct_err("bad_pwd2", hint="请输入安全码后再删除")
        _bad = _acct_check_code(e, val)
        if _bad is not None:
            return _bad
        try:
            await emby.emby_del(e.embyid)
        except Exception as ex:
            LOGGER.warning(f"【MiniApp】删除 Emby 用户失败 {e.name}: {ex}")
        try:
            sql_delete_emby(e.tg)
        except Exception as ex:
            LOGGER.warning(f"【MiniApp】删除数据库记录失败 {e.name}: {ex}")
        return JSONResponse({"ok": True})

    return _acct_err("bad_act")


@route.get("/api/devices", include_in_schema=False)
async def miniapp_devices(request: Request):
    """我的设备：与机器人「我的设备」同源（Emby PlaybackActivity 播放记录）"""
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    uid = str(getattr(e, "embyid", "") or "")
    items = []
    if uid:
        sql = ("SELECT DeviceName, ClientName, RemoteAddress, MAX(DateCreated) AS last, COUNT(*) AS n "
               "FROM PlaybackActivity WHERE UserId = '" + uid.replace("'", "") + "' "
               "GROUP BY DeviceName, ClientName, RemoteAddress ORDER BY last DESC")
        try:
            r = await emby._request("POST", "/emby/user_usage_stats/submit_custom_query",
                                    json={"CustomQueryString": sql, "ReplaceUserId": False})
            rows = ((getattr(r, "data", None) or {}).get("results")) or []
            for row in rows[:50]:
                items.append({
                    "dev": (row[0] or "未知设备"),
                    "app": (row[1] or ""),
                    "ip": (row[2] or ""),
                    "last": str(row[3] or ""),
                    "n": int(row[4] or 0),
                    "playing": False,
                    "title": "",
                    "id": "",
                })
        except Exception as ex:
            LOGGER.warning(f"【MiniApp】读取登录设备失败: {ex}")
    try:
        sr = await emby._request("GET", "/emby/Sessions")
        for sess in (getattr(sr, "data", None) or []):
            if str(sess.get("UserId") or "").replace("-", "") != uid.replace("-", ""):
                continue
            dev = sess.get("DeviceName") or sess.get("Client") or ""
            np = sess.get("NowPlayingItem") or {}
            for it in items:
                if dev and it["dev"] == dev:
                    it["id"] = sess.get("Id") or ""
                    if np:
                        it["playing"] = True
                        it["title"] = np.get("Name") or ""
                    break
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】读取在线会话失败: {ex}")
    devs = {(it["dev"], it["app"]) for it in items}
    ips = {it["ip"] for it in items if it["ip"]}
    return JSONResponse({"ok": True, "items": items, "count": len(devs), "ips": len(ips)})

@route.post("/api/devices/kick", include_in_schema=False)
async def miniapp_devices_kick(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("id") or "").strip()
    if not sid:
        return _acct_err("bad_id")
    uid = str(getattr(e, "embyid", "") or "")
    try:
        sr = await emby._request("GET", "/emby/Sessions")
        mine = None
        for sess in (getattr(sr, "data", None) or []):
            if str(sess.get("Id") or "") == sid:
                mine = sess
                break
        if mine is None:
            return _acct_err("not_online", hint="该设备已不在线，无需下线")
        if str(mine.get("UserId") or "").replace("-", "") != uid.replace("-", ""):
            return _acct_err("not_yours", hint="只能下线自己的设备")
        try:
            if mine.get("NowPlayingItem"):
                await emby._request("POST", f"/emby/Sessions/{sid}/Playing/Stop")
        except Exception:
            pass
        await emby._request("POST", f"/emby/Sessions/{sid}/Logout")
        return JSONResponse({"ok": True})
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】踢设备失败: {ex}")
        return _acct_err("kick_failed")


@route.post("/api/redeem", include_in_schema=False)
async def miniapp_redeem(request: Request):
    """兑换码：续期码 / 白名单码 / 分区码（逻辑与机器人 exchange 一致）"""
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    try:
        from bot.sql_helper.sql_code import sql_get_code, sql_update_code
    except Exception:
        sql_get_code = sql_update_code = None
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = str(body.get("code") or "").strip()
    if not raw:
        return _acct_err("bad_code", hint="请输入兑换码")
    uid = int(u.get("id") or 0)
    now = _acct_dt.now()

    def _mk_used(code_text):
        try:
            sql_update_code(code_text, uid, now)
        except Exception as ex:
            LOGGER.warning(f"【MiniApp】标记兑换码已用失败: {ex}")
        try:
            _bag_log(uid, "code", "", code_text)
        except Exception:
            pass

    # ---- 1. Rcode 表：续期码 / 白名单码 / 注册码 ----
    c = None
    hit = raw
    for cand in (raw, raw.upper(), raw.replace(" ", "")):
        try:
            c = sql_get_code(cand)
        except Exception:
            c = None
        if c:
            hit = cand
            break
    if c:
        if int(c.used or 0) > 0:
            return _acct_err("code_used", hint="该兑换码已被使用")
        code_text = str(getattr(c, "code", hit) or hit)
        low = code_text.lower()
        if low.startswith("whitelist"):
            try:
                from bot import _open as _acct_open
                if not getattr(_acct_open, "use_whitelist_code", True):
                    return _acct_err("whitelist_closed", hint="管理员未开启白名单码")
            except Exception:
                pass
            if not e.embyid:
                return _acct_err("no_account", hint="请先在机器人注册 Emby 账户")
            if str(e.lv) == "a":
                return _acct_err("already_wl", hint="您已是白名单用户")
            try:
                ok = sql_update_emby(Emby.tg == e.tg, lv="a")
            except Exception as ex:
                LOGGER.warning(f"【MiniApp】白名单升级失败: {ex}")
                ok = False
            if not ok:
                return _acct_err("save_failed")
            _mk_used(code_text)
            return JSONResponse({"ok": True, "kind": "whitelist", "hint": "白名单已激活"})
        if low.startswith("renew"):
            if not e.embyid:
                return _acct_err("no_account", hint="请先在机器人注册 Emby 账户")
            days = int(c.us or 0)
            if days <= 0:
                return _acct_err("code_invalid", hint="兑换码无效")
            cur = e.ex or now
            expired = now > cur
            new_ex = (now if expired else cur) + _acct_td(days=days)
            upd = {"ex": new_ex}
            if expired and str(e.lv) == "c":
                upd["lv"] = "b"
            try:
                ok = sql_update_emby(Emby.tg == e.tg, **upd)
            except Exception as ex:
                LOGGER.warning(f"【MiniApp】续期失败: {ex}")
                ok = False
            if not ok:
                return _acct_err("save_failed")
            _mk_used(code_text)
            if expired:
                try:
                    await emby.emby_change_policy(emby_id=e.embyid, disable=False)
                except Exception as ex:
                    LOGGER.warning(f"【MiniApp】恢复账号失败: {ex}")
            return JSONResponse({"ok": True, "kind": "renew", "days": days,
                                 "expire": new_ex.strftime("%Y-%m-%d %H:%M")})
        return _acct_err("register_code", hint="这是注册码，您已有账户，无需使用")

    # ---- 2. partition_codes 表：分区码 ----
    pcode = None
    try:
        from bot.sql_helper.sql_partition import sql_get_partition_code as _acct_pcode
        pcode = _acct_pcode(raw) or _acct_pcode(raw.upper())
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】查询分区码失败: {ex}")
    if pcode:
        if not e.embyid:
            return _acct_err("no_account", hint="请先在机器人注册 Emby 账户")
        libs = []
        try:
            from bot import partition_libs as _acct_plibs
            libs = (_acct_plibs or {}).get(getattr(pcode, "partition", ""), []) or []
        except Exception:
            libs = []
        try:
            ok, part, exp = _acct_redeem(code=str(getattr(pcode, "code", raw)), tg=uid,
                                         embyid=e.embyid, embyname=(e.name or ""), now=now)
        except Exception as ex:
            LOGGER.warning(f"【MiniApp】分区码兑换失败: {ex}")
            ok, part, exp = False, None, None
        if not ok or not exp:
            return _acct_err("code_used", hint="分区码无效或已被使用")
        if libs:
            try:
                await emby.show_folders_by_names(e.embyid, libs)
            except Exception as ex:
                LOGGER.warning(f"【MiniApp】同步媒体库权限失败: {ex}")
        return JSONResponse({"ok": True, "kind": "partition", "partition": part or "",
                             "expire": exp.strftime("%Y-%m-%d") if hasattr(exp, "strftime") else str(exp),
                             "libs": libs})
    return _acct_err("code_not_found", hint="兑换码不存在")

# ================= MINIAPP_ACCT_END =================

# ================= MINIAPP_STATS_V1 资源统计（今日更新 / 总资源） =================
_STATS_TTL = 300
_stats_cache = {"ts": 0.0, "data": None}


async def _stats_build():
    import datetime as _dt
    now = _dt.datetime.utcnow()
    bj = now + _dt.timedelta(hours=8)
    midnight = bj.replace(hour=0, minute=0, second=0, microsecond=0)
    start = (midnight - _dt.timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S")

    counts = await emby._request("GET", "/emby/Items/Counts")
    cd = counts.data if (counts and getattr(counts, "success", False)) else {}

    async def _day_count(kind):
        try:
            r = await emby._request("GET", "/emby/Items", params={
                "Recursive": "true",
                "IncludeItemTypes": kind,
                "MinDateCreated": start,
                "Limit": 1,
                "EnableTotalRecordCount": "true",
            })
            if not (r and getattr(r, "success", False)):
                return 0
            return int((r.data or {}).get("TotalRecordCount") or 0)
        except Exception as ex:
            LOGGER.warning(f"【MiniApp】今日更新统计失败({kind}): {ex}")
            return 0

    eps = await _day_count("Episode")
    mov = await _day_count("Movie")
    mov_all = int(cd.get("MovieCount") or 0)
    ser_all = int(cd.get("SeriesCount") or 0)
    eps_all = int(cd.get("EpisodeCount") or 0)
    return {
        "eps": eps,
        "movies": mov,
        "total": mov_all + ser_all + eps_all,
        "movie_all": mov_all,
        "series_all": ser_all,
        "episode_all": eps_all,
        "day": midnight.strftime("%Y-%m-%d"),
    }


@route.get("/api/stats", include_in_schema=False)
async def miniapp_stats(request: Request):
    init_data = (request.headers.get("X-Tg-Init-Data")
                 or request.query_params.get("initData") or "")
    if not _verify_init_data(init_data):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    import time as _t
    now_ts = _t.time()
    cached = _stats_cache.get("data")
    if cached and now_ts - float(_stats_cache.get("ts") or 0) < _STATS_TTL:
        out = dict(cached)
        out["ok"] = True
        out["cached"] = True
        return JSONResponse(out)
    try:
        data = await _stats_build()
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】资源统计失败: {ex}")
        return JSONResponse({"ok": False, "error": "stats_failed"})
    _stats_cache["ts"] = now_ts
    _stats_cache["data"] = data
    out = dict(data)
    out["ok"] = True
    out["cached"] = False
    return JSONResponse(out)

# ================= MINIAPP_STATS_V1 END =================

# ================= MINIAPP_BTN3_V1 START =================
def _b3_ts(v):
    t = str(v or "").strip()
    return t.replace("T", " ")[:19]


def _b3_hm(sec):
    sec = int(sec or 0)
    if sec >= 3600:
        return "%.1f h" % (sec / 3600.0)
    return "%d 分钟" % max(1, sec // 60)


@route.get("/api/points/log", include_in_schema=False)
async def miniapp_points_log(request: Request):
    """奶茶增减记录：签到(+)/求片(-)，历史无法追溯的部分单独标注"""
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    tg = int(u.get("id") or 0)
    items = []
    try:
        from bot.sql_helper import engine
        from sqlalchemy import text
        with engine.connect() as c:
            rows = c.execute(text("SELECT day, pts, ts FROM emby_checkin WHERE tg = :tg ORDER BY ts DESC LIMIT 60"),
                             {"tg": tg}).fetchall()
            for r in rows:
                items.append({"ts": _b3_ts(r[2]), "d": int(r[1] or 0), "src": "checkin", "note": "每日签到"})
            rows = c.execute(text("SELECT request_name, cost, create_at FROM request_records WHERE tg = :tg ORDER BY create_at DESC LIMIT 60"),
                             {"tg": tg}).fetchall()
            for r in rows:
                items.append({"ts": _b3_ts(r[2]), "d": -abs(int(r[1] or 0)), "src": "request", "note": str(r[0] or "求片")[:36]})
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】奶茶流水读取失败: {ex}")
    items.sort(key=lambda x: x["ts"], reverse=True)
    plus = sum(i["d"] for i in items if i["d"] > 0)
    minus = -sum(i["d"] for i in items if i["d"] < 0)
    _emby_before = int(getattr(e, "iv", 0) or 0)
    _bal = _emby_before - plus + minus
    untracked = _bal if _bal > 0 else 0
    return JSONResponse({"ok": True, "bal": _emby_before, "items": items[:60],
                         "plus": plus, "minus": minus, "untracked": untracked,
                         "note": "可追溯到签到与求片，其余为活动赠送/兑换调整"})


@route.get("/api/watch/top", include_in_schema=False)
async def miniapp_watch_top(request: Request, days: int = 30):
    """近 N 天看了哪些剧、各看了多久（按剧聚合）"""
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    try:
        days = max(1, min(365, int(days)))
    except Exception:
        days = 30
    uid = str(getattr(e, "embyid", "") or "")
    if not uid:
        return _acct_err("not_registered")
    since = datetime.utcfromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d %H:%M:%S")
    sql = ("SELECT ItemId, SUM(PlayDuration - PauseDuration) AS s, MAX(DateCreated) AS last, COUNT(*) AS n "
           "FROM PlaybackActivity WHERE UserId = '%s' AND DateCreated >= '%s' "
           "GROUP BY ItemId ORDER BY s DESC LIMIT 80") % (uid, since)
    rows = []
    try:
        r = await emby._request("POST", "/emby/user_usage_stats/submit_custom_query",
                                json={"CustomQueryString": sql, "ReplaceUserId": False})
        rows = ((getattr(r, "data", None) or {}).get("results")) or []
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】观影TOP查询失败: {ex}")
    ids = [str(x[0]) for x in rows if x and x[0]]
    meta = {}
    if ids:
        try:
            for i in range(0, len(ids), 20):
                part = ids[i:i + 20]
                ir = await emby._request("GET", "/emby/Items?Fields=SeriesName,SeriesId,ProductionYear&Ids=" + ",".join(part))
                for it in ((getattr(ir, "data", None) or {}).get("Items") or []):
                    sid = str(it.get("Id") or "")
                    meta[sid] = {"name": it.get("Name") or "", "series": it.get("SeriesName") or "",
                                 "type": it.get("Type") or "", "sid": str(it.get("SeriesId") or "")}
        except Exception as ex:
            LOGGER.warning(f"【MiniApp】观影TOP取名称失败: {ex}")
    agg = {}
    for row in rows:
        iid = str(row[0] or "")
        sec = int(row[1] or 0)
        m = meta.get(iid) or {}
        key = m.get("series") or m.get("name") or iid
        a = agg.get(key)
        if not a:
            a = {"name": key, "sec": 0, "eps": set(), "last": "", "id": m.get("sid") or iid,
                 "single": bool(m) and not m.get("series")}
            agg[key] = a
        a["sec"] += sec
        a["eps"].add(iid)
        last = _b3_ts(row[2])
        if last > a["last"]:
            a["last"] = last
    out = []
    for a in agg.values():
        out.append({"name": a["name"], "sec": a["sec"], "eps": len(a["eps"]),
                    "last": a["last"], "id": a["id"], "hm": _b3_hm(a["sec"])})
    out.sort(key=lambda x: -x["sec"])
    total = sum(x["sec"] for x in out)
    return JSONResponse({"ok": True, "days": days, "items": out[:20], "total": total,
                         "total_hm": _b3_hm(total)})


@route.get("/api/playing", include_in_schema=False)
async def miniapp_playing(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    myid = str(getattr(e, "embyid", "") or "")
    lv = str(getattr(e, "lv", "") or "")
    limit = 4 if lv == "a" else 1
    items = []
    try:
        r = await emby._request("GET", "/emby/Sessions")
        sess = getattr(r, "data", None) or []
        for s0 in sess:
            if str(s0.get("UserId") or "") != myid:
                continue
            np = s0.get("NowPlayingItem")
            if not np:
                continue
            ps = s0.get("PlayState") or {}
            pos = int(ps.get("PositionTicks") or 0) / 10000000.0
            dur = int(np.get("RunTimeTicks") or 0) / 10000000.0
            typ = np.get("Type") or ""
            series = np.get("SeriesName") or ""
            name = np.get("Name") or ""
            sub = ""
            if typ == "Episode":
                sn = np.get("ParentIndexNumber")
                en = np.get("IndexNumber")
                if sn and en:
                    sub = "第%d季 第%d集" % (int(sn), int(en))
                elif en:
                    sub = "第%d集" % int(en)
            items.append({
                "sid": str(s0.get("Id") or ""),
                "dev": s0.get("DeviceName") or "未知设备",
                "app": s0.get("Client") or s0.get("ApplicationVersion") or "",
                "title": series or name or "未知",
                "name": name if series else "",
                "sub": sub,
                "pos": round(pos, 1),
                "dur": round(dur, 1),
                "pct": int(round(100.0 * pos / dur)) if dur > 0 else 0,
                "paused": bool(ps.get("IsPaused")),
                "playing": True,
                "remote": (s0.get("RemoteEndPoint") or "").split(":")[0],
            })
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】正在播放查询失败: {ex}")
    items.sort(key=lambda x: -x["pos"])
    return JSONResponse({"ok": True, "items": items, "count": len(items),
                         "limit": limit, "lv": lv, "cached": False,
                         "lv_n": 1, "lv_w": 4,
                         "lv_name": ("白名单" if lv == "a" else "普通")})

@route.post("/api/playing/stop", include_in_schema=False)
async def miniapp_playing_stop(request: Request):
    """强制结束自己的一路播放"""
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    uid = str(getattr(e, "embyid", "") or "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("id") or "").strip()
    if not sid:
        return _acct_err("bad_id")
    try:
        sr = await emby._request("GET", "/emby/Sessions")
        ok = False
        for sess in (getattr(sr, "data", None) or []):
            if str(sess.get("Id") or "") == sid and str(sess.get("UserId") or "").replace("-", "") == uid.replace("-", ""):
                ok = True
                break
        if not ok:
            return _acct_err("not_yours", hint="只能结束自己的播放")
        await emby._request("POST", "/emby/Sessions/%s/Playing/Stop" % sid)
        return JSONResponse({"ok": True})
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】强制结束播放失败: {ex}")
        return _acct_err("stop_failed", hint="结束播放失败，请稍后重试")

# ================= MINIAPP_BTN3_V1 END =================


# ================= MINIAPP_MALL_V1 商城 / 系统设置 =================
import json as _mmjson
from datetime import datetime as _mmdt, timedelta as _mmtd
try:
    from sqlalchemy import text as _mmtext
    from bot.sql_helper import engine as _mm_engine
    from bot.func_helper.utils import cr_link_two as _mm_cr_link_two
except Exception as _mm_imp_err:  # pragma: no cover
    _mmtext = None
    _mm_engine = None
    _mm_cr_link_two = None
    LOGGER.warning(f"【MiniApp】mall import error: {_mm_imp_err}")

_MM_DDL = [
    """CREATE TABLE IF NOT EXISTS miniapp_mall(
        id INT AUTO_INCREMENT PRIMARY KEY,
        sku VARCHAR(32) NOT NULL UNIQUE,
        name VARCHAR(64) NOT NULL,
        descr VARCHAR(255) NOT NULL DEFAULT '',
        icon VARCHAR(16) NOT NULL DEFAULT '',
        price INT NOT NULL DEFAULT 0,
        stock INT NOT NULL DEFAULT 0,
        on_sale TINYINT NOT NULL DEFAULT 1,
        sort INT NOT NULL DEFAULT 0,
        payload TEXT,
        updated_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS miniapp_order(
        id INT AUTO_INCREMENT PRIMARY KEY,
        tg BIGINT NOT NULL,
        sku VARCHAR(32) NOT NULL,
        name VARCHAR(64) NOT NULL DEFAULT '',
        price INT NOT NULL DEFAULT 0,
        result VARCHAR(255) NOT NULL DEFAULT '',
        created DATETIME NULL,
        KEY idx_tg(tg)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS miniapp_title(
        tg BIGINT PRIMARY KEY,
        title VARCHAR(32) NOT NULL DEFAULT '',
        updated DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS miniapp_kv(
        k VARCHAR(48) PRIMARY KEY,
        v TEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

_MM_SEED = [
    ("renew", "续期卡", "延长账号到期时间", "🎫", 300, 20, 10, {"days": 30}),
    ("whitelist", "白名单卡", "解锁 4 路并发播放", "🚀", 500, 10, 20, {}),
    ("partition", "分区卡", "解锁独立分区媒体库", "📚", 400, 10, 30, {"days": 30, "partition": ""}),
    ("invite", "邀请码卡", "生成好友注册码", "🎁", 200, 30, 40, {"days": 30}),
    ("title", "自定义称号卡", "兑换后可自定义显示称号", "🏷️", 150, 50, 50, {}),
    ("unban", "解封卡", "解除封禁，恢复普通权限", "🔓", 600, 5, 60, {}),
]

_MM_SYS_DEF = {"ann_title": "", "ann_text": "", "support": "@JingCaiDiJiaBot", "maintenance": "0"}


def _mm_rows(sql, **kw):
    if _mmtext is None or _mm_engine is None:
        return []
    try:
        with _mm_engine.connect() as c:
            return [dict(r) for r in c.execute(_mmtext(sql), kw).mappings().all()]
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】mall query error: {ex}")
        return []


def _mm_x(sql, **kw):
    """执行写操作，返回受影响行数；异常返回 -1"""
    if _mmtext is None or _mm_engine is None:
        return -1
    try:
        with _mm_engine.connect() as c:
            r = c.execute(_mmtext(sql), kw)
            c.commit()
            return int(r.rowcount)
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】mall exec error: {ex}")
        return -1


def _mm_init():
    if _mmtext is None or _mm_engine is None:
        return
    for q in _MM_DDL:
        _mm_x(q)
    try:
        n = _mm_rows("SELECT COUNT(*) AS n FROM miniapp_mall")
        if not n or int(n[0]["n"]) == 0:
            for sku, name, descr, icon, price, stock, sort, payload in _MM_SEED:
                _mm_x("INSERT IGNORE INTO miniapp_mall(sku,name,descr,icon,price,stock,on_sale,sort,payload,updated_at)"
                      " VALUES(:sku,:name,:descr,:icon,:price,:stock,1,:sort,:payload,NOW())",
                      sku=sku, name=name, descr=descr, icon=icon, price=price, stock=stock,
                      sort=sort, payload=_mmjson.dumps(payload, ensure_ascii=False))
    except Exception as ex:
        LOGGER.warning(f"【MiniApp】mall seed error: {ex}")


try:
    _mm_init()
except Exception as _mm_err:
    LOGGER.warning(f"【MiniApp】mall init failed: {_mm_err}")


def _mm_admin(u):
    try:
        return int((u or {}).get("id") or 0) in set(getattr(config, "admins", []) or [])
    except Exception:
        return False


def _mm_items(admin=False):
    sql = "SELECT * FROM miniapp_mall"
    if not admin:
        sql += " WHERE on_sale=1"
    sql += " ORDER BY sort ASC, id ASC"
    out = []
    for r in _mm_rows(sql):
        try:
            pl = _mmjson.loads(r.get("payload") or "{}")
        except Exception:
            pl = {}
        out.append({
            "id": int(r["id"]), "sku": r["sku"], "name": r["name"], "descr": r.get("descr") or "",
            "icon": r.get("icon") or "", "price": int(r.get("price") or 0), "stock": int(r.get("stock") or 0),
            "on_sale": bool(r.get("on_sale")), "sort": int(r.get("sort") or 0), "payload": pl,
            "updated": str(r.get("updated_at") or ""),
        })
    return out


def _mm_item(sku):
    rows = _mm_rows("SELECT * FROM miniapp_mall WHERE sku=:s LIMIT 1", s=sku)
    if not rows:
        return None
    r = rows[0]
    try:
        pl = _mmjson.loads(r.get("payload") or "{}")
    except Exception:
        pl = {}
    r["payload"] = pl
    return r


def _mm_sys():
    d = dict(_MM_SYS_DEF)
    for r in _mm_rows("SELECT k,v FROM miniapp_kv"):
        d[str(r["k"])] = r.get("v") or ""
    return d


def _mm_sys_set(k, v):
    return _mm_x("INSERT INTO miniapp_kv(k,v) VALUES(:k,:v) ON DUPLICATE KEY UPDATE v=VALUES(v)", k=k, v=str(v))


def _mm_title(tg):
    rows = _mm_rows("SELECT title FROM miniapp_title WHERE tg=:t LIMIT 1", t=int(tg))
    return (rows[0]["title"] if rows else "") or ""


def _mm_titles_all():
    """{tg: title} 全量自定义称号，一次查询，榜单用。"""
    out = {}
    try:
        for r in _mm_rows("SELECT tg, title FROM miniapp_title"):
            t = (r.get("title") or "").strip()
            if t:
                try:
                    out[int(r["tg"])] = t
                except Exception:
                    pass
    except Exception as exc:
        LOGGER.warning(f"【MiniApp】自定义称号批量读取失败: {exc}")
    return out


@route.get("/api/mall", include_in_schema=False)
async def miniapp_mall_list(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    admin = _mm_admin(u)
    return JSONResponse({"ok": True, "admin": admin, "items": _mm_items(admin),
                         "bal": int(getattr(e, "iv", 0) or 0) if e else 0,
                         "lv": (getattr(e, "lv", "") if e else "") or "",
                         "title": _mm_title(u.get("id")) if e else ""})


@route.post("/api/mall/save", include_in_schema=False)
async def miniapp_mall_save(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not _mm_admin(u):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    sku = str(body.get("sku") or "").strip()
    if not sku:
        return _acct_err("bad_sku")
    it = _mm_item(sku)
    if not it:
        return _acct_err("no_item")
    sets, params = [], {"s": sku}
    if "price" in body:
        try:
            params["price"] = max(0, int(body.get("price")))
        except Exception:
            return _acct_err("bad_price", hint="价格必须是数字")
        sets.append("price=:price")
    if "stock" in body:
        try:
            params["stock"] = max(0, int(body.get("stock")))
        except Exception:
            return _acct_err("bad_stock", hint="库存必须是数字")
        sets.append("stock=:stock")
    if "on_sale" in body:
        params["on"] = 1 if body.get("on_sale") else 0
        sets.append("on_sale=:on")
    if "name" in body:
        nm = str(body.get("name") or "").strip()[:64]
        if not nm:
            return _acct_err("bad_name")
        params["name"] = nm
        sets.append("name=:name")
    if "descr" in body:
        params["descr"] = str(body.get("descr") or "").strip()[:255]
        sets.append("descr=:descr")
    if "icon" in body:
        params["icon"] = str(body.get("icon") or "").strip()[:16]
        sets.append("icon=:icon")
    if "days" in body:
        try:
            days = max(1, min(3650, int(body.get("days"))))
        except Exception:
            return _acct_err("bad_days", hint="天数必须是数字")
        pl = dict(it.get("payload") or {})
        pl["days"] = days
        params["payload"] = _mmjson.dumps(pl, ensure_ascii=False)
        sets.append("payload=:payload")
    if not sets:
        return _acct_err("nothing", hint="没有需要保存的内容")
    sets.append("updated_at=NOW()")
    n = _mm_x("UPDATE miniapp_mall SET " + ", ".join(sets) + " WHERE sku=:s", **params)
    if n < 0:
        return _acct_err("save_failed", hint="保存失败，请稍后再试")
    return JSONResponse({"ok": True, "items": _mm_items(True)})


@route.post("/api/mall/buy", include_in_schema=False)
async def miniapp_mall_buy(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not e:
        return _acct_err("not_registered")
    try:
        body = await request.json()
    except Exception:
        body = {}
    sku = str(body.get("sku") or "").strip()
    note = str(body.get("note") or "").strip()[:32]
    it = _mm_item(sku)
    if not it:
        return _acct_err("no_item")
    if not it.get("on_sale"):
        return _acct_err("off_sale", hint="该商品已下架")
    if int(it.get("stock") or 0) <= 0:
        return _acct_err("out_of_stock", hint="库存不足，已售罄")
    price = int(it.get("price") or 0)
    bal = int(getattr(e, "iv", 0) or 0)
    if bal < price:
        return _acct_err("no_bal", hint=f"奶茶不足，还差 {price - bal}")
    lv = (getattr(e, "lv", "") or "").lower()
    if sku == "title" and not (2 <= len(note) <= 12):
        return _acct_err("bad_note", hint="请输入 2-12 个字的称号")
    if sku == "unban" and lv not in ("c", "d"):
        return _acct_err("not_banned", hint="账号当前未被封禁，无需解封")
    if sku == "whitelist" and lv == "a":
        return _acct_err("already_wl", hint="你已经是白名单用户了")
    # 原子扣库存
    n = _mm_x("UPDATE miniapp_mall SET stock=stock-1, updated_at=NOW() WHERE sku=:s AND stock>0 AND on_sale=1", s=sku)
    if n != 1:
        return _acct_err("out_of_stock", hint="库存不足，已售罄")
    try:
        n2 = _mm_x("UPDATE emby SET iv=iv-:p WHERE tg=:t AND iv>=:p", p=price, t=int(u["id"]))
        if n2 != 1:
            raise RuntimeError("奶茶不足")
        pl = it.get("payload") or {}
        code, msg = "", ""
        if sku == "renew":
            days = int(pl.get("days") or 30)
            base = getattr(e, "ex", None) or _mmdt.now()
            if isinstance(base, str):
                try:
                    base = _mmdt.strptime(base[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    base = _mmdt.now()
            if base < _mmdt.now():
                base = _mmdt.now()
            _mm_x("UPDATE emby SET ex=:x WHERE tg=:t", x=base + _mmtd(days=days), t=int(u["id"]))
            msg = f"已续期 {days} 天"
        elif sku == "whitelist":
            _mm_x("UPDATE emby SET lv='a' WHERE tg=:t", t=int(u["id"]))
            msg = "已升级为白名单，4 路并发"
        elif sku == "partition":
            days = int(pl.get("days") or 30)
            part = str(pl.get("partition") or "").strip()
            if not part:
                raise RuntimeError("分区卡未配置分区名")
            from bot.sql_helper.sql_partition import sql_upsert_partition_grant as _mm_grant
            _mm_grant(int(u["id"]), getattr(e, "embyid", "") or "", part,
                      _mmdt.now() + _mmtd(days=days), f"mall-{sku}", getattr(e, "name", "") or "")
            msg = f"已开通分区「{part}」{days} 天"
        elif sku == "invite":
            days = int(pl.get("days") or 30)
            from bot.func_helper.utils import pwd_create as _mm_pwd_create
            from bot.sql_helper.sql_code import sql_add_code as _mm_add_code
            from bot import ranks as _mm_ranks
            _mm_p = await _mm_pwd_create(10)
            code = "%s-%d-Register_%s" % (str(_mm_ranks.logo), int(time.time()), _mm_p)
            if _mm_add_code([code], int(u["id"]), days) is False:
                raise RuntimeError("邀请码生成失败")
            link = "t.me/%s?start=%s" % (str(getattr(config, "bot_name", "") or ""), code)
            msg = f"邀请码已生成，好友注册可得 {days} 天"
        elif sku == "title":
            _mm_x("INSERT INTO miniapp_title(tg,title,updated) VALUES(:t,:v,NOW())"
                  " ON DUPLICATE KEY UPDATE title=VALUES(title),updated=VALUES(updated)",
                  t=int(u["id"]), v=note)
            msg = f"称号已更新为「{note}」"
        elif sku == "unban":
            _mm_x("UPDATE emby SET lv='b' WHERE tg=:t", t=int(u["id"]))
            if getattr(e, "embyid", ""):
                try:
                    await emby.emby_change_policy(str(e.embyid), disable=False)
                except Exception as _ex:
                    LOGGER.warning(f"【MiniApp】解封后启用 Emby 用户失败: {_ex}")
            msg = "已解除封禁，恢复普通权限"
        else:
            raise RuntimeError("商品暂不可用")
        _bag_used = 0 if (sku == "invite" and code) else 1
        _mm_x("INSERT INTO miniapp_order(tg,sku,name,price,result,used,created) VALUES(:t,:s,:n,:p,:r,:u,NOW())",
              t=int(u["id"]), s=sku, n=it.get("name") or sku, p=price, r=(code or msg)[:255], u=_bag_used)
    except Exception as ex:
        _mm_x("UPDATE miniapp_mall SET stock=stock+1 WHERE sku=:s", s=sku)
        _mm_x("UPDATE emby SET iv=iv+:p WHERE tg=:t", p=price, t=int(u["id"]))
        LOGGER.warning(f"【MiniApp】商城兑换失败 {sku}: {ex}")
        return _acct_err("buy_failed", hint=str(ex)[:60] or "兑换失败，奶茶已退回")
    bal_now = _mm_rows("SELECT iv FROM emby WHERE tg=:t LIMIT 1", t=int(u["id"]))
    return JSONResponse({"ok": True, "msg": msg, "code": code,
                         "bal": int(bal_now[0]["iv"]) if bal_now else bal - price,
                         "sku": sku, "stock": max(0, int(it.get("stock") or 0) - 1)})


@route.get("/api/sys", include_in_schema=False)
async def miniapp_sys_get(request: Request):
    u, e = _acct_ctx(request)
    admin = _mm_admin(u)
    s = _mm_sys()
    return JSONResponse({"ok": True, "admin": admin, "sys": s,
                         "ver": str(getattr(config, "version", "2.0.0") or "2.0.0"),
                         "title": _mm_title(u.get("id")) if u and e else ""})


@route.post("/api/sys/save", include_in_schema=False)
async def miniapp_sys_save(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not _mm_admin(u):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    n = 0
    for k in ("ann_title", "ann_text", "support", "maintenance"):
        if k in body:
            v = body.get(k)
            if k == "maintenance":
                v = "1" if v in (1, "1", True, "true", "on") else "0"
            else:
                v = str(v or "").strip()[:400]
            if _mm_sys_set(k, v) >= 0:
                n += 1
    return JSONResponse({"ok": True, "saved": n, "sys": _mm_sys()})


@route.get("/api/mall/orders", include_in_schema=False)
async def miniapp_mall_orders(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    rows = _mm_rows("SELECT sku,name,price,result,created FROM miniapp_order WHERE tg=:t ORDER BY id DESC LIMIT 20",
                    t=int(u["id"]))
    return JSONResponse({"ok": True, "items": [{"sku": r["sku"], "name": r["name"], "price": int(r["price"]),
                                               "result": r["result"], "at": str(r["created"] or "")} for r in rows]})
# ================= MINIAPP_MALL_V1 END =================

# ===== MiniApp 背包 =====
def _bag_ts(v):
    try:
        if v is None:
            return ""
        if hasattr(v, "strftime"):
            try:
                v = v + __import__("datetime").timedelta(hours=8)
            except Exception:
                pass
            return v.strftime("%Y-%m-%d %H:%M")
        return str(v)[:16].replace("T", " ")
    except Exception:
        return ""


def _bag_kind(code_text):
    low = str(code_text or "").lower()
    if low.startswith("whitelist"):
        return "白名单码"
    if low.startswith("renew"):
        return "续期码"
    if low.startswith("partition"):
        return "分区码"
    return "兑换码"


def _bag_log(tg, sku, name, result=""):
    """记录一条背包条目（已使用）"""
    if _mm_engine is None:
        return
    try:
        nm = _bag_kind(result) if sku == "code" else (name or sku)
        _mm_x("INSERT INTO miniapp_order(tg,sku,name,price,result,used,used_at,created) "
             "VALUES(:t,:s,:n,0,:r,1,NOW(),NOW())",
             t=int(tg), s=sku, n=nm, r=str(result or "")[:255])
    except Exception:
        pass


try:
    _mm_x("ALTER TABLE miniapp_order ADD COLUMN used TINYINT NOT NULL DEFAULT 1")
except Exception:
    pass
try:
    _mm_x("ALTER TABLE miniapp_order ADD COLUMN used_at DATETIME NULL")
except Exception:
    pass


def _bag_item(r):
    _bi_sku = str(r.get("sku") or "")
    _bi_res = str(r.get("result") or "")
    _bi_link = ""
    if _bi_sku == "invite" and _bi_res:
        _bi_link = _bi_res if _bi_res.startswith("http") else (
            "https://t.me/%s?start=%s" % (str(getattr(config, "bot_name", "") or ""), _bi_res))
    return {
        "id": int(r.get("id") or 0),
        "sku": _bi_sku,
        "link": _bi_link,
        "name": str(r.get("name") or ""),
        "price": int(r.get("price") or 0),
        "result": str(r.get("result") or ""),
        "used": int(r.get("used") if r.get("used") is not None else 1),
        "at": _bag_ts(r.get("created")),
    }


@route.get("/api/bag", include_in_schema=False)
async def miniapp_bag(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    tg = int(u["id"])
    rows = _mm_rows("SELECT id,sku,name,price,result,used,created FROM miniapp_order "
                    "WHERE tg=:t ORDER BY id DESC LIMIT 100", t=tg)
    unused, used = [], []
    for r in rows:
        d = _bag_item(r)
        (unused if d["used"] == 0 else used).append(d)
    bal = _mm_rows("SELECT iv FROM emby WHERE tg=:t LIMIT 1", t=tg)
    return JSONResponse({
        "ok": True,
        "bal": int(bal[0]["iv"]) if bal and bal[0].get("iv") is not None else 0,
        "unused": unused,
        "used": used,
    })


@route.post("/api/bag/use", include_in_schema=False)
async def miniapp_bag_use(request: Request):
    u, e = _acct_ctx(request)
    if not u:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        oid = int(body.get("id") or 0)
    except Exception:
        oid = 0
    if not oid:
        return JSONResponse({"ok": False, "error": "bad_id", "hint": "参数错误"})
    tg = int(u["id"])
    rows = _mm_rows("SELECT id,sku,name,result,used FROM miniapp_order WHERE id=:i AND tg=:t LIMIT 1",
                    i=oid, t=tg)
    if not rows:
        return JSONResponse({"ok": False, "error": "not_yours", "hint": "该物品不存在"})
    it = rows[0]
    if str(it.get("sku") or "") == "invite":
        _iu = str(it.get("result") or "")
        _ilink = _iu if _iu.startswith("http") else (
            "https://t.me/%s?start=%s" % (str(getattr(config, "bot_name", "") or ""), _iu))
        return JSONResponse({"ok": True, "copy": _ilink, "link": _ilink,
                             "msg": "邀请链接已复制", "name": str(it.get("name") or ""),
                             "result": _iu})
    if int(it.get("used") or 0) != 0:
        return JSONResponse({"ok": False, "error": "already_used", "hint": "该物品已使用"})
    n = _mm_x("UPDATE miniapp_order SET used=1, used_at=NOW() WHERE id=:i AND tg=:t", i=oid, t=tg)
    if n < 1:
        return JSONResponse({"ok": False, "error": "use_failed", "hint": "操作失败，请重试"})
    return JSONResponse({"ok": True, "msg": "已使用", "name": str(it.get("name") or ""),
                         "result": str(it.get("result") or "")})

