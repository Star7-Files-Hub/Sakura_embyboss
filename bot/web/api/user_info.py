#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
get_user_info -
Author:susu
Date:2024/8/27
"""

import json
from fastapi import APIRouter, Request
from bot.sql_helper import Session
from bot.sql_helper.sql_emby import Emby, sql_get_emby, sql_update_emby
from bot.schemas.schemas import MAX_INT_VALUE, MIN_INT_VALUE
from bot.func_helper.emby import emby
from bot import LOGGER, group, bot

route = APIRouter()

# 单次积分变动上限：防止一次请求把积分推到 32 位整数边界附近。
# 如确有更大额度的运营需求，可调高此常量（但不要超过 MAX_INT_VALUE）。
MAX_CREDIT_DELTA = 1_000_000


@route.get("/user_info")
async def user_info(tg: str):
    # 从数据库获取用户信息
    user = sql_get_emby(tg)

    if not user:
        return {"code": 404, "message": "用户不存在"}
    return {"code": 200, "data": {"tg": user.tg, "iv": user.iv, "name": user.name, "embyid": user.embyid, "lv": user.lv, "cr": user.cr, "ex": user.ex}}


@route.post("/update_credit")
async def update_credit(request: Request):
    """
    修改用户积分
    :param request: 请求对象
    """
    try:
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, str):
                data = json.loads(data)
        else:
            form_data = await request.form()
            data = json.loads(form_data["data"]) if "data" in form_data else {}

        tg = data.get("tg")
        credit = data.get("credit")
        if not tg or credit is None:
            return {"code": 400, "message": "参数错误"}

        # tg 必须是整数，避免把任意字符串当作主键查询条件
        try:
            tg = int(tg)
        except (TypeError, ValueError):
            return {"code": 400, "message": "参数错误"}

        # credit 必须是整数，且有单次变动上限
        try:
            delta = int(credit)
        except (TypeError, ValueError):
            return {"code": 400, "message": "积分变动必须为整数"}
        if delta == 0:
            return {"code": 400, "message": "积分变动不能为 0"}
        if abs(delta) > MAX_CREDIT_DELTA:
            return {"code": 400, "message": f"单次积分变动不得超过 {MAX_CREDIT_DELTA}"}

        # 获取用户信息
        user = sql_get_emby(tg)
        if not user:
            return {"code": 404, "message": "用户不存在"}

        # 计算新的积分值（用于提前给出友好错误）
        new_iv = user.iv + delta
        if not (MIN_INT_VALUE <= new_iv <= MAX_INT_VALUE):
            return {"code": 400, "message": "积分超出允许范围"}
        if new_iv < 0:
            return {"code": 400, "message": "积分不足"}

        # 使用数据库侧原子自增，避免"读-算-写绝对值"在并发下丢失更新；
        # WHERE 中带上边界条件，使余额校验与写入成为同一个原子操作。
        try:
            with Session() as session:
                updated = (
                    session.query(Emby)
                    .filter(
                        Emby.tg == tg,
                        Emby.iv + delta >= 0,
                        Emby.iv + delta <= MAX_INT_VALUE,
                    )
                    .update({Emby.iv: Emby.iv + delta}, synchronize_session=False)
                )
                session.commit()
        except Exception as e:
            LOGGER.error(f"更新用户 {tg} 积分失败: {e}")
            return {"code": 500, "message": "更新失败"}

        if not updated:
            # 行不存在，或并发下余额已不足以扣减
            return {"code": 400, "message": "积分不足或用户不存在"}

        # 回读真实余额（并发下 user.iv + delta 可能已不是最新值）
        refreshed = sql_get_emby(tg)
        current_iv = refreshed.iv if refreshed else new_iv
        return {
            "code": 200,
            "data": {"tg": tg, "iv": current_iv, "changed": delta},
        }
    except json.JSONDecodeError:
        return {"code": 400, "message": "无效的JSON格式"}
    except Exception as e:
        # 不把异常原文回传给调用方，避免泄露内部路径与驱动信息
        LOGGER.error(f"update_credit 处理失败: {e}")
        return {"code": 500, "message": "服务器内部错误"}

@route.post("/ban")
async def ban_user(request: Request):
    """
    封禁用户
    :param request: 请求对象
    """
    try:
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, str):
                data = json.loads(data)
        else:
            form_data = await request.form()
            data = json.loads(form_data["data"]) if "data" in form_data else {}

        query = data.get("query")
        if not query:
            return {"code": 400, "message": "参数错误"}

        # 获取用户信息 query 可以是 tg 或 embyname 或 embyid
        user = sql_get_emby(tg = query)
        if not user or not user.embyid:
            return {"code": 404, "message": "用户不存在"}
        
        disable_emby = await emby.emby_change_policy(emby_id=user.embyid, disable=True)
        
        if disable_emby:
            # 更新用户等级为封禁状态
            user.lv = 'c'  # 封禁状态
            sql_update_emby(Emby.tg == user.tg, lv='c')
            send_notification = f"#BAN通告\n用户 {user.name} (TG: #{user.tg}, EmbyID: {user.embyid}) 已被封禁。"
            LOGGER.info(send_notification)
            await bot.send_message(chat_id=group[0], text=send_notification)
            return {
                "code": 200,
                "data": {"tg": user.tg,"embyid": user.embyid, "name": user.name, "lv": user.lv},
            }
        else:
            return {"code": 500, "message": "封禁失败"}
    except json.JSONDecodeError:
        return {"code": 400, "message": "无效的JSON格式"}
    except Exception as e:
        # 不把异常原文回传给调用方，避免泄露内部路径与驱动信息
        LOGGER.error(f"ban_user 处理失败: {e}")
        return {"code": 500, "message": "服务器内部错误"}
