import asyncio
import random
from datetime import datetime, timezone, timedelta

from pyrogram import filters

from bot import bot, _open, sakura_b
from bot.func_helper.filters import user_in_group_on_filter
from bot.func_helper.msg_utils import callAnswer, sendMessage, deleteMessage
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby


@bot.on_callback_query(filters.regex('checkin') & user_in_group_on_filter)
async def user_in_checkin(_, call):
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    if _open.checkin:
        e = sql_get_emby(call.from_user.id)
        if not e:
            await callAnswer(call, '🧮 未查询到数据库', True)

        elif not e.ch or e.ch.strftime("%Y-%m-%d") < today:
            if _open.checkin_lv:
                if e.lv > _open.checkin_lv:
                    await callAnswer(call, f'❌ 您无权签到，如有异议，请不要有异议。', True)
                    return
            rewards = _open.checkin_rewards
            if rewards:
                reward = random.choices(
                    [int(item[0]) for item in rewards],
                    weights=[float(item[1]) for item in rewards],
                    k=1,
                )[0]
            else:
                reward = random.randint(_open.checkin_reward[0], _open.checkin_reward[1])
            s = e.iv + reward
            sql_update_emby(Emby.tg == call.from_user.id, iv=s, ch=now)
            try:  # 同步写入签到明细，供会员中心日历展示
                from sqlalchemy import text as _ckt
                from bot.sql_helper.sql_emby import Session as _CkS
                with _CkS() as _s:
                    _s.execute(_ckt(
                        "CREATE TABLE IF NOT EXISTS emby_checkin ("
                        "tg BIGINT NOT NULL, day DATE NOT NULL, pts INT NOT NULL DEFAULT 0, "
                        "ts DATETIME NOT NULL, PRIMARY KEY (tg, day)"
                        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))
                    _s.execute(_ckt(
                        "INSERT INTO emby_checkin (tg, day, pts, ts) VALUES (:t,:d,:p,:s) "
                        "ON DUPLICATE KEY UPDATE pts=:p"),
                        {"t": call.from_user.id, "d": today, "p": int(reward),
                         "s": now.strftime("%Y-%m-%d %H:%M:%S")})
                    _s.commit()
            except Exception:
                pass
            text = f'🎉 **签到成功** | {reward} {sakura_b}\n💴 **当前持有** | {s} {sakura_b}\n⏳ **签到日期** | {now.strftime("%Y-%m-%d")}'
            await asyncio.gather(deleteMessage(call), sendMessage(call, text=text))

        else:
            await callAnswer(call, '⭕ 您今天已经签到过了！签到是无聊的活动哦。', True)
    else:
        await callAnswer(call, '❌ 未开启签到功能，等待！', True)
