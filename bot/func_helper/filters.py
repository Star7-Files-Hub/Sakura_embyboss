#!/usr/bin/python3
from pyrogram.errors import BadRequest
from pyrogram.filters import create
from bot import admins, owner, group, LOGGER
from pyrogram.enums import ChatMemberStatus


# async def owner_filter(client, update):
#     """
#     过滤 owner
#     :param client:
#     :param update:
#     :return:
#     """
#     user = update.from_user or update.sender_chat
#     uid = user.id
#     return uid == owner

# 三个参数给on用
async def admins_on_filter(filt, client, update) -> bool:
    """
    过滤admins中id，包括owner
    :param client:
    :param update:
    :return:
    """
    user = update.from_user or update.sender_chat
    uid = user.id
    # 注意：不要在此处加入 `uid in group`。group 是授权群/频道的 chat id（值为负），uid 正常
    # 是用户 id（值为正），取值域不相交，对实名用户恒为 False；唯一例外是"匿名管理员"
    # （uid 取到 sender_chat.id，恰为某个授权群 id），那会让"在授权群匿名发言"直接拿到管理
    # 权限，属绕过 admins 白名单，故一并移除（A-C2）。群成员判定请走 user_in_group_on_filter。
    return bool(uid == owner or uid in admins)


async def admins_filter(update):
    """
    过滤admins中id，包括owner
    """
    user = update.from_user or update.sender_chat
    uid = user.id
    return bool(uid == owner or uid in admins)


async def user_in_group_filter(client, update):
    """
    过滤在授权组中的人员
    :param client:
    :param update:
    :return:
    """
    uid = update.from_user or update.sender_chat
    uid = uid.id
    for i in group:
        try:
            u = await client.get_chat_member(chat_id=int(i), user_id=uid)
            if u.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER, ChatMemberStatus.OWNER, ChatMemberStatus.RESTRICTED]:
                return True
        except BadRequest as e:
            if e.ID == 'USER_NOT_PARTICIPANT':
                # 用户不在当前群，继续检查后续授权群（多群部署下不能提前判定失败）
                continue
            elif e.ID == 'CHAT_ADMIN_REQUIRED':
                LOGGER.error(f"bot不能在 {i} 中工作，请检查bot是否在群组及其权限设置")
                return False
            else:
                return False
        else:
            continue
    return False


async def user_in_group_on_filter(filt, client, update):
    """
    过滤在授权组中的人员
    :param client:
    :param update:
    :return:
    """
    uid = update.from_user or update.sender_chat
    uid = uid.id
    # 注意：不要在此处加入 `uid in group`。group 是授权群/频道的 chat id（值为负），uid 正常
    # 是用户 id（值为正），取值域不相交，对实名用户恒为 False；唯一例外是"匿名管理员"
    # （uid 取到 sender_chat.id，恰为某个授权群 id），那等于凭"发言所在群"放行而非校验群成员，
    # 属绕过（A-C2 一并移除）。群成员判定只能靠下面逐个群调用 get_chat_member。
    for i in group:
        try:
            u = await client.get_chat_member(chat_id=int(i), user_id=uid)
            if u.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER,
                            ChatMemberStatus.OWNER]:  # 移除了 'ChatMemberStatus.RESTRICTED' 防止有人进群直接注册不验证
                return True  # 因为被限制用户无法使用bot，所以需要检查权限。
        except BadRequest as e:
            if e.ID == 'USER_NOT_PARTICIPANT':
                # 用户不在当前群，继续检查后续授权群（多群部署下不能提前判定失败）
                continue
            elif e.ID == 'CHAT_ADMIN_REQUIRED':
                LOGGER.error(f"bot不能在 {i} 中工作，请检查bot是否在群组及其权限设置")
                return False
            else:
                return False
    return False


# 过滤 on_message or on_callback 的admin
admins_on_filter = create(admins_on_filter)
admins_filter = create(admins_filter)

# 过滤 是否在群内
user_in_group_f = create(user_in_group_filter)
user_in_group_on_filter = create(user_in_group_on_filter)
