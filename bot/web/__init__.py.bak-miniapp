#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
__init__.py - 
Author:susu
Date:2024/8/27
"""
import errno

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .api import emby_api_route, user_api_route, auth_api_route
from bot import api as config_api, LOGGER

__all__ = ["Web", "check"]

# 单个请求体的最大字节数（默认 64KB）。
# Emby webhook 的 JSON 远小于此值；限制可避免超大请求体带来的内存与解析压力。
MAX_REQUEST_BODY_BYTES = 64 * 1024


class Web:

    """
    Web 类用于初始化和管理 FastAPI 应用程序。
    """

    def __init__(self):
        """
        初始化 Web 类实例。
        """
        # 交互式文档默认关闭：/docs、/redoc、/openapi.json 无鉴权即可访问，
        # 会泄露全部端点、参数与模型结构。需要时在 config.json 中设置 api.expose_docs = true。
        if getattr(config_api, "expose_docs", False):
            self.app: FastAPI = FastAPI()
        else:
            self.app: FastAPI = FastAPI(
                docs_url=None,
                redoc_url=None,
                openapi_url=None,
            )
        self.web_api = None
        self.start_api = None

    def init_api(self):
        """
        初始化 API 路由和 CORS 中间件。
        """
        # 添加路由 /
        self.app.include_router(emby_api_route)
        self.app.include_router(user_api_route)
        self.app.include_router(auth_api_route)
        # 配置 CORS 中间件
        # 默认不放行任何跨域来源（allow_origins 未配置时为空列表）。
        # 注意：本 API 的凭据是显式令牌而非 Cookie，因此不要使用 "*" 通配，
        # 否则任意站点都能跨域调用并读取响应。
        allow_origins = [o for o in (config_api.allow_origins or []) if o != "*"]
        if len(allow_origins) != len(config_api.allow_origins or []):
            LOGGER.warning("【API服务】allow_origins 中的 \"*\" 已被忽略，请显式列出允许的来源域名。")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=allow_origins,  # 来源白名单，支持多个反代域名
            allow_credentials=bool(allow_origins),  # 仅在配置了明确来源时才允许携带凭证
            allow_methods=["*"],  # 允许跨域的方法
            allow_headers=["*"])  # 允许的请求头

        # 请求体大小上限：所有 webhook 都直接 await request.json()，
        # 未限制体积时可以用超大请求体造成内存/解析压力。
        @self.app.middleware("http")
        async def _limit_request_body(request, call_next):
            content_length = request.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BODY_BYTES:
                LOGGER.warning(f"拒绝超大请求体: {content_length} 字节 from {request.client.host if request.client else 'unknown'}")
                return JSONResponse(
                    status_code=413,
                    content={"status": "error", "message": "请求体过大"},
                )
            return await call_next(request)

    async def start(self):
        """
        启动 Web API 服务。
        """
        if not config_api.status:
            LOGGER.info("【API服务】未配置，跳过...")
            return
        LOGGER.info("【API服务】检测有配置，马上启动服务...")
        import uvicorn

        self.init_api()
        self.web_api = uvicorn.Server(
            config=uvicorn.Config(self.app, host=config_api.http_url, port=config_api.http_port)
        )
        server_config = self.web_api.config
        if not server_config.loaded:
            server_config.load()  # 加载配置
        self.web_api.lifespan = server_config.lifespan_class(server_config)
        try:
            await self.web_api.startup()
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                LOGGER.error(f"【API服务】端口 {config_api.http_port} 被占用，请修改配置文件.")
            LOGGER.error("【API服务】启动失败，退出ing...")
            raise SystemExit from None
        if self.web_api.should_exit:
            LOGGER.error("【API服务】启动失败，退出ing...")
            raise SystemExit from None

        LOGGER.info("【API服务】 启动成功!")

    async def stop(self):
        """
        停止 Web API 服务。
        """
        if not self.web_api:
            return
        LOGGER.info("正在停止 API 服务...")
        try:
            await self.web_api.shutdown()
        except Exception as e:
            LOGGER.error(f"停止 API 服务时出错: {e}")
        finally:
            LOGGER.info("API 服务已停止。")


check = Web()

# 说明：这里刻意不在模块导入期调度 check.start()。
# 导入期调用 asyncio.get_event_loop() 已被弃用，且若 bot 之后使用了不同的事件循环，
# 该任务会永远不被执行（或落在一个已关闭的循环上）。
# API 服务改由 main.py 的启动钩子显式 await 启动。
