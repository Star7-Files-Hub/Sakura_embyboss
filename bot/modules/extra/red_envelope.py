"""
red_envelope - 

Author:susu
Date:2023/01/02
"""

import cn2an
import asyncio
import random
import math
from datetime import datetime, timedelta
from pyrogram import filters
from pyrogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func

from bot import bot, prefixes, sakura_b, bot_photo, red_envelope, _open, LOGGER
from bot.func_helper.filters import user_in_group_on_filter
from bot.func_helper.fix_bottons import users_iv_button
from bot.func_helper.msg_utils import sendPhoto, sendMessage, callAnswer, editMessage
from bot.func_helper.utils import pwd_create, judge_admins, get_users, cache
from bot.sql_helper import Session
from bot.sql_helper.sql_emby import Emby, sql_get_emby, sql_update_emby
from bot.ranks_helper.ranks_draw import RanksDraw
from bot.schemas import Yulv, MAX_INT_VALUE, MIN_INT_VALUE

# 小项目，说实话不想写数据库里面。放内存里了，从字典里面每次拿分

red_envelopes = {}


class RedEnvelope:
    def __init__(self, money, members, sender_id, sender_name, envelope_type="random"):
        self.id = None
        self.money = money  # 总金额
        self.rest_money = money  # 剩余金额
        self.members = members  # 总份数
        self.rest_members = members  # 剩余份数
        self.sender_id = sender_id  # 发送者ID
        self.sender_name = sender_name  # 发送者名称
        self.type = envelope_type  # random/equal/private
        self.receivers = {}  # {user_id: {"amount": xx, "name": "xx"}}
        self.target_user = None  # 专享红包接收者ID
        self.message = None  # 红包消息（普通红包和专享红包共用）
        # 每个红包一把锁：串行化"校验是否已领取 → 扣减池子 → 入账"整个过程，
        # 避免同一用户快速连点导致重复领取/池子被扣成负数（TOCTOU）。
        self.lock = asyncio.Lock()


async def create_reds(
    money,
    members,
    first_name,
    sender_id,
    envelope_type="random",
    private=None,
    private_text=None,
):
    red_id = await pwd_create(5)
    envelope = RedEnvelope(
        money=money,
        members=members,
        sender_id=sender_id,
        sender_name=first_name,
        envelope_type=envelope_type,
    )

    if private:
        envelope.type = "private"
        envelope.target_user = private
    if private_text is None:
        # 专享红包：如果没有传入祝福语，则随机选择默认祝福语
        envelope.message = random.choice(Yulv.load_yulv().red_bag)
    else:
        envelope.message = private_text
    envelope.id = red_id
    red_envelopes[red_id] = envelope

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="👆🏻 好運連連", callback_data=f"red_envelope-{red_id}"
                )
            ]
        ]
    )


@bot.on_message(
    filters.command("red", prefixes) & user_in_group_on_filter & filters.group
)
async def send_red_envelope(_, msg):
    if not red_envelope.status:
        return await asyncio.gather(
            msg.delete(), sendMessage(msg, "🚫 红包功能已关闭！")
        )

    if not red_envelope.allow_private and msg.reply_to_message:
        return await asyncio.gather(
            msg.delete(), sendMessage(msg, "🚫 专属红包功能已关闭！")
        )

    # 处理专享红包
    if msg.reply_to_message and red_envelope.allow_private:
        target_from_user = msg.reply_to_message.from_user
        target_sender_chat = msg.reply_to_message.sender_chat

        # 不允许对机器人或频道发送专属红包
        if (target_from_user and target_from_user.is_bot) or target_sender_chat:
            return await asyncio.gather(
                msg.delete(),
                sendMessage(msg, "🚫 专属红包不能发给机器人或频道!", timer=60),
            )

        try:
            money = int(msg.command[1])
            private_text = (
                msg.command[2]
                if len(msg.command) > 2
                else random.choice(Yulv.load_yulv().red_bag)
            )
        except (IndexError, ValueError):
            return await asyncio.gather(
                msg.delete(),
                sendMessage(
                    msg,
                    "**🧧 专享红包：\n\n请回复某人 [数额] [祝福语（可选）]**",
                    timer=60,
                ),
            )

        # 验证发送者资格
        if msg.reply_to_message and red_envelope.allow_private:
            try:
                money = int(msg.command[1])
                private_text = (
                    msg.command[2]
                    if len(msg.command) > 2
                    else random.choice(Yulv.load_yulv().red_bag)
                )
            except (IndexError, ValueError):
                return await asyncio.gather(
                    msg.delete(),
                    sendMessage(
                        msg,
                        "**🧧 专享红包：\n\n请回复某人 [数额] [祝福语（可选）]**",
                        timer=60,
                    ),
                )

            verified, first_name, error = await verify_red_envelope_sender(
                msg, money, is_private=True
            )
            if not verified:
                return

        # 创建并发送红包
        reply, _ = await asyncio.gather(
            msg.reply("正在准备专享红包，稍等"), msg.delete()
        )

        ikb = await create_reds(
            money=money,
            members=1,
            first_name=first_name,
            sender_id=msg.from_user.id if not msg.sender_chat else msg.sender_chat.id,
            private=msg.reply_to_message.from_user.id,
            private_text=private_text,
        )

        user_pic = await get_user_photo(msg.reply_to_message.from_user)
        cover = await RanksDraw.hb_test_draw(
            money, 1, user_pic, f"{msg.reply_to_message.from_user.first_name} 专享"
        )

        sign_name = f'{msg.sender_chat.title}' if msg.sender_chat else f'[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'
        await asyncio.gather(
            sendPhoto(msg, photo=cover, buttons=ikb),
            reply.edit(
                f"🔥 [{msg.reply_to_message.from_user.first_name}]"
                f"(tg://user?id={msg.reply_to_message.from_user.id})\n"
                f"您收到一个来自 {sign_name} 的专属红包"
            )
        )
        return

    # 处理普通红包
    try:
        money = int(msg.command[1])
        members = int(msg.command[2])
    except (IndexError, ValueError):
        return await asyncio.gather(
            msg.delete(),
            sendMessage(
                msg,
                f"**🧧 发红包：\n\n/red [总{sakura_b}数] [份数] [mode] [祝福语（可选）]**\n\n"
                f"[mode]填 1 为拼手气, 其他值为均分\n[祝福语]不传则随机默认祝福语\n专享红包请回复 + {sakura_b}",
                timer=60,
            ),
        )

    # 验证发送者资格和红包参数
    verified, first_name, error = await verify_red_envelope_sender(msg, money)
    if not verified:
        return

    # 创建并发送红包
    mode_param = msg.command[3] if len(msg.command) > 3 else None
    envelope_type = "random" if (mode_param is None or str(mode_param) == "1") else "equal"
    private_text = msg.command[4] if len(msg.command) > 4 else None
    reply, _ = await asyncio.gather(msg.reply("正在准备红包，稍等"), msg.delete())

    ikb = await create_reds(
        money=money,
        members=members,
        first_name=first_name,
        sender_id=msg.from_user.id if not msg.sender_chat else msg.sender_chat.id,
        envelope_type=envelope_type,
        private_text=private_text
    )

    user_pic = await get_user_photo(msg.from_user if not msg.sender_chat else msg.chat)
    cover = await RanksDraw.hb_test_draw(money, members, user_pic, first_name)

    await asyncio.gather(sendPhoto(msg, photo=cover, buttons=ikb), reply.delete())


# 锚定正则：只匹配领取按钮的 `red_envelope-{id}`。
# 原写法 filters.regex("red_envelope") 会同时命中 config_panel 的
# `set_red_envelope_status` / `set_red_envelope_allow_private`（它们含 "red_envelope" 子串），
# 导致本 handler 被误触发并在 call.data.split("-")[1] 处抛 IndexError。
@bot.on_callback_query(filters.regex(r"^red_envelope-") & user_in_group_on_filter)
async def grab_red_envelope(_, call):
    try:
        red_id = call.data.split("-", 1)[1]
    except IndexError:
        return await callAnswer(call, "无效的红包数据。", True)
    try:
        envelope = red_envelopes[red_id]
    except (IndexError, KeyError):
        return await callAnswer(
            call, "/(ㄒoㄒ)/~~ \n\n来晚了，红包已经被抢光啦。", True
        )

    # 验证用户资格
    e = sql_get_emby(tg=call.from_user.id)
    if not e:
        return await callAnswer(call, "你还未私聊bot! 数据库没有你.", True)

    # 校验、扣减池子与入账必须在同一临界区内完成，
    # 否则并发点击会同时通过"已领取"检查，造成重复领取与池子被扣成负数。
    async with envelope.lock:
        # 检查是否已领取
        if call.from_user.id in envelope.receivers:
            return await callAnswer(call, "ʕ•̫͡•ʔ 你已经领取过红包了。不许贪吃", True)

        # 检查红包是否已抢完
        if envelope.rest_members <= 0:
            return await callAnswer(
                call, "/(ㄒoㄒ)/~~ \n\n来晚了，红包已经被抢光啦。", True
            )

        amount = 0
        # 处理均分红包
        if envelope.type == "equal":
            amount = envelope.money // envelope.members

        # 处理专享红包
        elif envelope.type == "private":
            if call.from_user.id != envelope.target_user:
                return await callAnswer(call, "ʕ•̫͡•ʔ 这是你的专属红包吗？", True)
            amount = envelope.rest_money
            await callAnswer(
                call,
                f"🧧恭喜，你领取到了\n{envelope.sender_name} の {amount}{sakura_b}\n\n{envelope.message}",
                True,
            )

        # 处理拼手气红包
        else:
            if envelope.rest_members > 1:
                k = 2 * envelope.rest_money / envelope.rest_members
                amount = int(random.uniform(1, k))
            else:
                amount = envelope.rest_money

        # 金额合法性兜底：池子不足或算出非正数时拒绝，避免负数入账
        if amount <= 0 or amount > envelope.rest_money:
            LOGGER.error(
                f"红包金额异常，已拒绝发放: red_id={red_id}, amount={amount}, "
                f"rest_money={envelope.rest_money}"
            )
            return await callAnswer(call, "红包数据异常，请联系管理员。", True)

        # 更新用户余额：数据库侧原子自增，避免"读-算-写绝对值"丢失更新
        new_balance = e.iv + amount
        if new_balance > MAX_INT_VALUE or new_balance < MIN_INT_VALUE:
            return await callAnswer(call, f"账户余额超出安全范围（{MIN_INT_VALUE} 到 {MAX_INT_VALUE}）。", True)
        try:
            with Session() as session:
                updated = (
                    session.query(Emby)
                    .filter(Emby.tg == call.from_user.id, Emby.iv + amount <= MAX_INT_VALUE)
                    .update({Emby.iv: Emby.iv + amount}, synchronize_session=False)
                )
                session.commit()
        except Exception as ex:
            LOGGER.error(f"红包入账失败: tg={call.from_user.id}, amount={amount}, error={ex}")
            updated = 0
        if not updated:
            return await callAnswer(call, "领取失败，请稍后再试。", True)

        # 更新红包信息
        envelope.receivers[call.from_user.id] = {
            "amount": amount,
            "name": call.from_user.first_name or "Anonymous",
        }
        envelope.rest_money -= amount
        envelope.rest_members -= 1

    await callAnswer(
        call, f"🧧恭喜，你领取到了\n{envelope.sender_name} の {amount}{sakura_b}", True
    )

    # 处理红包抢完后的展示
    if envelope.rest_members == 0:
        red_envelopes.pop(red_id)
        text = await generate_final_message(envelope)
        n = 2048
        chunks = [text[i : i + n] for i in range(0, len(text), n)]
        for i, chunk in enumerate(chunks):
            if i == 0:
                await editMessage(call, chunk)
            else:
                await call.message.reply(chunk)


async def verify_red_envelope_sender(msg, money, is_private=False):
    """验证发红包者资格

    Args:
        msg: 消息对象
        money: 红包金额
        is_private: 是否为专享红包

    Returns:
        tuple: (验证是否通过, 发送者名称, 错误信息)
    """
    # 说明：以群组/频道身份发言（匿名管理员）时，sender_chat 非空。
    # 这种身份无法归属到具体付款账号，此前直接放行且不扣费，
    # 等价于"凭空铸币"——领取者会拿到真实入账的积分。
    # 现在统一按 msg.from_user 做余额校验与扣费；无法确定付款人时直接拒绝。
    sender = msg.from_user
    if msg.sender_chat and sender is None:
        return False, None, "以群组/频道身份发红包已停用，请用个人身份发送"

    e = sql_get_emby(tg=sender.id)
    conditions = [
        e,  # 用户存在
        e.iv >= money if e else False,  # 余额充足
        money >= 5,  # 红包金额不小于5
        e.iv >= 5 if e else False,  # 持有金额不小于5
    ]

    if is_private:
        # 专享红包额外检查 不能发给自己
        target = msg.reply_to_message.from_user if (
            msg.reply_to_message and msg.reply_to_message.from_user
        ) else None
        if target is None:
            return False, None, "无法识别专享红包的接收者，请回复对方的消息后再发送"
        conditions.append(target.id != sender.id)
    else:
        # 普通红包额外检查：金额不小于份数
        try:
            members = int(msg.command[2])
        except (IndexError, TypeError, ValueError):
            return False, None, "红包份数格式不正确，用法：/red 金额 份数"
        conditions.append(money >= members)

    if not all(conditions):
        error_msg = (
            f"[{sender.first_name}](tg://user?id={sender.id}) "
            f"违反规则，禁言一分钟。\nⅰ 所持有{sakura_b}不得小于5\nⅱ 发出{sakura_b}不得小于5"
        )
        if is_private:
            error_msg += "\nⅲ 不许发自己"
        else:
            error_msg += "\nⅲ 未私聊过bot"

        await asyncio.gather(
            msg.delete(),
            msg.chat.restrict_member(
                sender.id,
                ChatPermissions(),
                datetime.now() + timedelta(minutes=1),
            ),
            sendMessage(msg, error_msg, timer=60),
        )
        return False, None, error_msg

    # 验证通过，扣除余额：数据库侧原子自减并带余额下限条件，
    # 避免并发发红包导致余额被扣成负数。
    try:
        with Session() as session:
            updated = (
                session.query(Emby)
                .filter(Emby.tg == sender.id, Emby.iv >= money)
                .update({Emby.iv: Emby.iv - money}, synchronize_session=False)
            )
            session.commit()
    except Exception as ex:
        LOGGER.error(f"扣除红包金额失败: tg={sender.id}, money={money}, error={ex}")
        updated = 0
    if not updated:
        return False, None, f"余额不足，发送失败（需要 {money}{sakura_b}）"

    return True, sender.first_name or "Anonymous", None


async def get_user_photo(user):
    """获取用户头像"""
    if not user.photo:
        return None
    return await bot.download_media(
        user.photo.big_file_id,
        in_memory=True,
    )


async def generate_final_message(envelope):
    """生成红包领取完毕的消息"""
    if envelope.type == "private":
        receiver = envelope.receivers[envelope.target_user]
        return (
            f"🧧 {sakura_b}红包\n\n**{envelope.message}\n\n"
            f"🕶️{envelope.sender_name} **的专属红包已被 "
            f"[{receiver['name']}](tg://user?id={envelope.target_user}) 领取"
        )

    # 排序领取记录
    sorted_receivers = sorted(
        envelope.receivers.items(), key=lambda x: x[1]["amount"], reverse=True
    )
    envelope.message = envelope.message[:50] + "..." if len(envelope.message) > 53 else envelope.message

    text = (
        f"🧧 {sakura_b}红包\n\n**{envelope.message}\n\n"
        f"😎 {envelope.sender_name} **的红包已经被抢光啦~\n\n"
    )

    for i, (user_id, details) in enumerate(sorted_receivers):
        if i == 0:
            text += f"**🏆 手气最佳 [{details['name']}](tg://user?id={user_id}) **获得了 {details['amount']} {sakura_b}"
        else:
            text += f"\n**[{details['name']}](tg://user?id={user_id})** 获得了 {details['amount']} {sakura_b}"

    return text


@bot.on_message(filters.command("srank", prefixes) & user_in_group_on_filter & filters.group)
async def s_rank(_, msg):
    await msg.delete()
    sender = None
    if not msg.sender_chat:
        e = sql_get_emby(tg=msg.from_user.id)
        if judge_admins(msg.from_user.id):
            sender = msg.from_user.id
        elif not e or e.iv < _open.srank_cost:
            await msg.delete()
            try:
                await msg.chat.restrict_member(
                    msg.from_user.id,
                    ChatPermissions(),
                    datetime.now() + timedelta(minutes=1),
                )
                await sendMessage(
                    msg,
                    f"[{msg.from_user.first_name}]({msg.from_user.id}) "
                    f"未私聊过bot或不足支付手续费{_open.srank_cost}{sakura_b}，禁言一分钟。",
                    timer=60,
                )
            except Exception as e:
                print(e)
            return
        else:
            sql_update_emby(Emby.tg == msg.from_user.id, iv=e.iv - _open.srank_cost)
            sender = msg.from_user.id
    elif msg.sender_chat.id == msg.chat.id:
        sender = msg.chat.id
    reply = await msg.reply(f"已扣除手续{_open.srank_cost}{sakura_b}, 请稍等......加载中")
    text, i = await users_iv_rank()
    t = "❌ 数据库操作失败" if not text else text[0]
    button = await users_iv_button(i, 1, sender or msg.chat.id)
    await asyncio.gather(
        reply.delete(),
        sendPhoto(
            msg,
            photo=bot_photo,
            caption=f"**▎🏆 {sakura_b}风云录**\n\n{t}",
            buttons=button,
        ),
    )


@cache.memoize(ttl=120)
async def users_iv_rank():
    with Session() as session:
        # 查询 Emby 表的所有数据，且>0 的条数
        p = session.query(func.count()).filter(Emby.iv > 0).scalar()
        if p == 0:
            return None, 1
        # 创建一个空字典来存储用户的 first_name 和 id
        members_dict = await get_users()
        i = math.ceil(p / 10)
        a = []
        b = 1
        m = ["🥇", "🥈", "🥉", "🏅"]
        # 分析出页数，将检索出 分割p（总数目）的 间隔，将间隔分段，放进【】中返回
        while b <= i:
            d = (b - 1) * 10
            # 查询iv排序，分页查询
            result = (
                session.query(Emby)
                .filter(Emby.iv > 0)
                .order_by(Emby.iv.desc())
                .limit(10)
                .offset(d)
                .all()
            )
            e = 1 if d == 0 else d + 1
            text = ""
            for q in result:
                name = str(members_dict.get(q.tg, q.tg))[:12]
                medal = m[e - 1] if e < 4 else m[3]
                text += f"{medal}**第{cn2an.an2cn(e)}名** | [{name}](google.com?q={q.tg}) の **{q.iv} {sakura_b}**\n"
                e += 1
            a.append(text)
            b += 1
        # a 是内容物，i是页数
        return a, i


# 检索翻页
@bot.on_callback_query(filters.regex(r"^users_iv:") & user_in_group_on_filter)
async def users_iv_pikb(_, call):
    # print(call.data)
    j, tg = map(int, call.data.split(":")[1].split("_"))
    if call.from_user.id != tg:
        if not judge_admins(call.from_user.id):
            return await callAnswer(
                call, "❌ 这不是你召唤出的榜单，请使用自己的 /srank", True
            )

    await callAnswer(call, f"将为您翻到第 {j} 页")
    a, b = await users_iv_rank()
    button = await users_iv_button(b, j, tg)
    text = a[j - 1]
    await editMessage(call, f"**▎🏆 {sakura_b}风云录**\n\n{text}", buttons=button)
