from datetime import datetime, timezone, timedelta
from pathlib import Path

from bot import bot, group, LOGGER
from bot.func_helper.emby import emby
from bot.func_helper.scheduler import scheduler

IMAGE = Path("/app/bot/scheduler/cinema.png")


def _media_label(item):
    kind = item.get("Type", "Unknown")
    if kind == "Episode":
        series = item.get("SeriesName")
        season = item.get("SeasonName")
        name = item.get("Name", "未知")
        if series:
            name = f"{series} ({season})·{name}" if season else f"{series}·{name}"
        return name, "剧集 📺"
    if kind == "Movie":
        return item.get("Name", "未知"), "电影 🎬"
    if kind == "Audio":
        return item.get("Name", "未知"), "音乐 🎵"
    return item.get("Name", "未知"), f"{kind} 🎥"


async def notify_watch_count():
    now = datetime.now(timezone(timedelta(hours=8)))
    try:
        result = await emby._request("GET", "/emby/Sessions")
        if not result.success or result.data is None:
            await bot.send_photo(
                group[0], str(IMAGE),
                caption=f"☁️ 茶百道｜实时动态｜让好片陪伴日常\n\n🔴 Emby 暂时无法获取观影数据\n\n🥤 奶茶负责解渴，好片负责解闷\n🚀 数据实时更新中……\n🕒 {now:%Y-%m-%d %H:%M}",
            )
            return

        sessions = result.data
        online_users = {s.get("UserId") for s in sessions if s.get("UserId")}
        playing = [s for s in sessions if s.get("NowPlayingItem")]
        caption = (
            f"☁️ 茶百道 ·让好片陪伴日常！\n\n"
            f"👥 当前在线人数: {len(online_users)} 人\n\n"
            f"🎬 当前播放: {len(playing)} 个\n\n"
            f"🥤 奶茶负责解渴，好片负责解闷\n\n"
            f"🚀 数据实时更新中……\n\n"
            f"🕒 {now:%Y-%m-%d %H:%M}"
        )
        await bot.send_photo(group[0], str(IMAGE), caption=caption)

        LOGGER.info(f"【watch_count】观影播报完成：在线{len(online_users)}人，播放{len(playing)}个会话")
    except Exception as exc:
        LOGGER.error(f"【watch_count】查询或通知失败：{exc}")


for job_id, hour, minute in [
    ("watch_count_0000", 0, 0),
    ("watch_count_1000", 10, 0),
    ("watch_count_1230", 12, 30),
    ("watch_count_1800", 18, 0),
    ("watch_count_2000", 20, 0),
    ("watch_count_2200", 22, 0),
]:
    scheduler.add_job(
        notify_watch_count, "cron", hour=hour, minute=minute,
        id=job_id, replace_existing=True,
    )
