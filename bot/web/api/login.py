#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
login.py - Emby用户登录接口
Author: susu
Date: 2025/01/06
"""

import json
import time
from typing import Dict, Optional, Tuple
from fastapi import APIRouter, Request 
from pydantic import BaseModel, Field, ValidationError

from bot.func_helper.emby import emby
from bot.sql_helper.sql_emby import Emby, sql_get_emby
from bot import LOGGER

router = APIRouter()


class LoginRequest(BaseModel):
    """登录请求模型"""
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    """登录响应模型"""
    code: int
    message: str
    data: Optional[dict] = None


# ---------------------------------------------------------------------------
# 登录失败节流：key = "来源IP|用户名小写"
# 说明：这是进程内的轻量防护，足以阻断在线口令爆破；
# 若部署了多个 worker，请改用 Redis 等共享存储。
# ---------------------------------------------------------------------------
_LOGIN_MAX_FAILURES = 5
_LOGIN_LOCK_SECONDS = 300
_LOGIN_MAX_LOCK_SECONDS = 1800
_LOGIN_FAILURES: Dict[str, Tuple[int, float]] = {}


def _login_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}|{username.lower()}"


def _login_locked(key: str) -> int:
    """返回剩余锁定秒数，0 表示未锁定。"""
    record = _LOGIN_FAILURES.get(key)
    if not record:
        return 0
    count, until = record
    if count < _LOGIN_MAX_FAILURES:
        return 0
    remaining = int(until - time.time())
    if remaining <= 0:
        _LOGIN_FAILURES.pop(key, None)
        return 0
    return remaining


def _login_record_failure(key: str) -> None:
    """记录一次失败；达到阈值后按指数退避锁定。"""
    count, _ = _LOGIN_FAILURES.get(key, (0, 0.0))
    count += 1
    until = 0.0
    if count >= _LOGIN_MAX_FAILURES:
        backoff = min(
            _LOGIN_LOCK_SECONDS * (2 ** (count - _LOGIN_MAX_FAILURES)),
            _LOGIN_MAX_LOCK_SECONDS,
        )
        until = time.time() + backoff
    _LOGIN_FAILURES[key] = (count, until)

    # 防止字典被大量随机用户名撑爆
    if len(_LOGIN_FAILURES) > 4096:
        now = time.time()
        for stale_key in [k for k, (c, u) in _LOGIN_FAILURES.items() if c < _LOGIN_MAX_FAILURES and u < now]:
            _LOGIN_FAILURES.pop(stale_key, None)


def _login_clear(key: str) -> None:
    _LOGIN_FAILURES.pop(key, None)


@router.post("/login", response_model=LoginResponse)
async def login(request: Request):
    """
    Emby用户登录接口
    
    Request Body:
    {
        "username": "user_name",
        "password": "user_password"
    }
    
    Success Response (200):
    {
        "code": 200,
        "message": "登录成功",
        "data": {
            "token": "xxxxxxxxxxxxx",
            "embyid": "user_id",
            "username": "user_name"
        }
    }
    
    Error Response:
    {
        "code": 401,
        "message": "用户名或密码错误"
    }
    """
    try:
        # 获取请求数据
        content_type = request.headers.get("content-type", "").lower()
        
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, str):
                data = json.loads(data)
        else:
            form_data = await request.form()
            data = json.loads(form_data.get("data", "{}")) if "data" in form_data else dict(form_data)

        if not isinstance(data, dict):
            return LoginResponse(code=400, message="请求参数不合法")

        # 用声明的模型做类型与长度校验（此前 LoginRequest 从未被使用）
        try:
            payload = LoginRequest(
                username=str(data.get("username") or "").strip(),
                password=str(data.get("password") or ""),
            )
        except ValidationError:
            return LoginResponse(code=400, message="用户名或密码格式不合法")

        username = payload.username
        password = payload.password

        # 失败节流：锁定期间直接拒绝，避免继续对 Emby 做口令校验
        key = _login_key(request, username)
        remaining = _login_locked(key)
        if remaining:
            LOGGER.warning(f"登录请求已被节流，来源 {request.client.host if request.client else 'unknown'}")
            return LoginResponse(
                code=429,
                message=f"尝试次数过多，请 {remaining} 秒后再试"
            )

        embyindb = sql_get_emby(username)
        if not embyindb:
            # 统一错误文案：不再区分"用户不存在"与"密码错误"，避免用户名枚举
            _login_record_failure(key)
            LOGGER.warning(f"Login attempt for non-existent user: {username}")
            return LoginResponse(
                code=401,
                message="用户名或密码错误"
            )
        
        # 调用Emby API进行身份验证
        success, embyid = await emby.authority_account(
            tg_id=0,
            username=username,
            password=password
        )
        
        if not success:
            _login_record_failure(key)
            LOGGER.warning(f"Login failed for user: {username}")
            return LoginResponse(
                code=401,
                message="用户名或密码错误"
            )

        _login_clear(key)
        LOGGER.info(f"User logged in successfully: {username}")
        
        return LoginResponse(
            code=200,
            message="登录成功",
            data={
                "embyid": str(embyid),
                "username": username
            }
        )
        
    except json.JSONDecodeError:
        LOGGER.error(f"Invalid JSON format in login request")
        return LoginResponse(
            code=400,
            message="无效的JSON格式"
        )
    except Exception as e:
        # 不把异常原文回传给调用方，避免泄露内部路径与驱动信息
        LOGGER.error(f"Login error: {str(e)}")
        return LoginResponse(
            code=500,
            message="服务器内部错误"
        )
