#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
__init__.py - 
Author:susu
Date:2024/8/27
"""
import secrets
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from .ban_playlist import route as ban_playlist_route
from .webhook.favorites import router as favorites_router
from .webhook.media import router as media_router
from .webhook.client_filter import router as client_filter_router
from .webhook.line_report import router as line_report_router
from .user_info import route as user_info_route
from .login import router as login_router
from bot import bot_token, LOGGER, api as config_api

emby_api_route = APIRouter(prefix="/emby", tags=["对接Emby的接口"])
user_api_route = APIRouter(prefix="/user", tags=["对接用户信息的接口"])
auth_api_route = APIRouter(prefix="/auth", tags=["用户认证接口"])

# 视为"本机"的来源地址：未配置 internal_token 时，仅这些地址可访问内部端点。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _token_matches(provided: Optional[str], expected: Optional[str]) -> bool:
    """恒定时间比较令牌，避免通过响应时间差逐字节猜测。"""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def verify_token(request: Request):
    """验证 API 请求的共享令牌。

    优先比对 api.internal_token（若已配置），否则回退 bot_token 以保持向后兼容。
    令牌可放在 `X-API-Token` 请求头（推荐）或 `?token=` 查询参数（兼容旧调用方）。

    注意：请优先使用请求头——查询参数形式的令牌会进入反代 access log 与浏览器历史。
    """
    try:
        token = request.headers.get("X-API-Token") or request.query_params.get("token")
        if not token:
            raise HTTPException(status_code=401, detail="No token provided")
        candidates = [t for t in (getattr(config_api, "internal_token", None), bot_token) if t]
        if not any(_token_matches(token, c) for c in candidates):
            # 不记录令牌内容，避免凭据泄露到日志
            LOGGER.warning(f"收到无效 API 令牌，来源 {_client_host(request)}")
            raise HTTPException(status_code=403, detail="Invalid token")
        return True
    except HTTPException:
        raise
    except Exception as e:
        LOGGER.error(f"Token verification error: {str(e)}")
        raise HTTPException(status_code=500, detail="Token verification failed")


async def verify_internal(request: Request):
    """内部端点鉴权（/emby/ban_playlist、/emby/line_report 专用）。

    这两个端点只应由同机反向代理调用，不能直接暴露给客户端：
    1) 若配置了 api.internal_token：要求请求头 X-Internal-Token 与之恒定时间相等；
    2) 未配置令牌时：仅允许来自回环地址（127.0.0.1 / ::1）的请求。

    若反代与 bot 不在同一网络命名空间（例如反代跑在 bridge 网络的容器里），
    其来源地址不是回环地址，此时必须显式配置 api.internal_token。
    """
    try:
        expected = getattr(config_api, "internal_token", None)
        if expected:
            provided = request.headers.get("X-Internal-Token", "")
            if not _token_matches(provided, expected):
                LOGGER.warning(f"内部端点令牌错误，来源 {_client_host(request)}")
                raise HTTPException(status_code=403, detail="Invalid internal token")
            return True

        if _client_host(request) not in _LOOPBACK_HOSTS:
            LOGGER.warning(
                f"拒绝非本机来源的内部端点访问：{_client_host(request)}。"
                "如需允许反代跨网络访问，请在 config.json 中配置 api.internal_token。"
            )
            raise HTTPException(status_code=403, detail="Internal endpoint: loopback access only")
        return True
    except HTTPException:
        raise
    except Exception as e:
        LOGGER.error(f"Internal token verification error: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal token verification failed")

async def verify_token(request: Request):
    """验证API请求的token"""
    try:
        # 从URL参数中获取token
        token = request.query_params.get("token")
        if not token:
            raise HTTPException(status_code=401, detail="No token provided")
        # 验证token是否与bot token匹配
        if token != bot_token:
            LOGGER.warning(f"Invalid token attempt: {token[:10]}...")
            raise HTTPException(status_code=403, detail="Invalid token")
        return True
    except HTTPException:
        raise
    except Exception as e:
        LOGGER.error(f"Token verification error: {str(e)}")
        raise HTTPException(status_code=500, detail="Token verification failed")

emby_api_route.include_router(
    ban_playlist_route,
    dependencies=[Depends(verify_internal)]
)
emby_api_route.include_router(
    favorites_router,
    dependencies=[Depends(verify_token)]
)
emby_api_route.include_router(
    media_router,
    dependencies=[Depends(verify_token)]
)
emby_api_route.include_router(
    client_filter_router,
    dependencies=[Depends(verify_token)]
)
emby_api_route.include_router(
    line_report_router,
    dependencies=[Depends(verify_internal)]
)
user_api_route.include_router(
    user_info_route,
    dependencies=[Depends(verify_token)]
)
auth_api_route.include_router(
    login_router,
    dependencies=[Depends(verify_token)]
)

