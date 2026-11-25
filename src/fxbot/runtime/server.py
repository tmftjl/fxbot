"""独立 ASGI 应用、健康检查与 OneBot 连接端点。"""

from __future__ import annotations

import hmac
import time
import asyncio
from contextlib import asynccontextmanager

from loguru import logger
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from fxbot.core.application import ApplicationState, DuplicateBotError

from .settings import Settings, SettingsState
from ..bootstrap import Runtime, create_runtime
from .connection import WireProtocol, ProtocolConnection
from ..console.routes import (
    auth,
    bots,
    chat,
    logs,
    meta,
    cache,
    stats,
    config,
    adapters,
    amemorix,
    personas,
    local_chat,
    membership,
    permissions,
    ai_providers,
)
from ..console.server import mount_console
from ..platforms.onebot11.protocol import OneBot11Protocol
from ..platforms.onebot12.protocol import OneBot12Protocol


def _elapsed(seconds: float) -> str:
    """连接时长只用于日志，秒级精度足够。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def create_app(settings: Settings, services: Runtime | None = None) -> FastAPI:
    """创建独立实例，不加载旧应用、数据库或业务插件。"""
    services = services if services is not None else create_runtime(settings)
    runtime = services.application

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动应用并回收全部连接和后台任务。"""
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.state.services = services
    # 令牌与 QQ 凭证可在控制台修改；新连接读取当前快照，监听地址仍需重启生效。
    app.state.settings = SettingsState(settings)
    app.state.adapter_lock = asyncio.Lock()

    @app.exception_handler(Exception)
    async def server_error(request: Request, error: Exception) -> JSONResponse:
        """HTTP 边界集中记录未知故障，响应不携带内部异常。"""
        logger.opt(exception=(type(error), error, error.__traceback__)).error("HTTP 请求失败")
        return JSONResponse(status_code=500, content={"detail": "服务暂时不可用"})

    @app.get("/health")
    async def health() -> dict[str, bool]:
        """仅返回实例是否就绪。"""
        return {"ok": runtime.state is ApplicationState.RUNNING}

    @app.post("/qq/webhook")
    async def qq_webhook(request: Request):
        return await services.qq.handle(request)

    @app.websocket("/onebot/v11/ws")
    async def onebot(websocket: WebSocket) -> None:
        """只接受通过令牌和身份校验的全双工连接。"""
        bot_id = websocket.headers.get("x-self-id", "")
        if websocket.headers.get("x-client-role", "").lower() != "universal":
            await websocket.close(code=1008)
            return
        await serve_connection(websocket, OneBot11Protocol(), bot_id)

    @app.websocket("/onebot/v12/{platform}/{bot_id}/ws")
    async def onebot12(websocket: WebSocket, platform: str, bot_id: str) -> None:
        """V12 使用标准子协议协商，身份从明确配置的连接路径绑定。"""
        protocols = websocket.scope.get("subprotocols", [])
        selected = next((value for value in protocols if value.startswith("12.") and len(value) > 3), None)
        if selected is None:
            await websocket.close(code=1008)
            return
        await serve_connection(websocket, OneBot12Protocol(platform), bot_id, selected)

    async def serve_connection(
        websocket: WebSocket,
        protocol: WireProtocol,
        bot_id: str,
        subprotocol: str | None = None,
    ) -> None:
        """认证和连接所有权共用一套流程，协议差异由协议对象处理。"""
        current = app.state.settings.current
        authorization = websocket.headers.get("authorization", "")
        if (
            current.token and not hmac.compare_digest(authorization.encode(), f"Bearer {current.token}".encode())
        ) or runtime.state is not ApplicationState.RUNNING:
            await websocket.close(code=1008)
            return
        disconnected = False

        async def close_transport(code: int) -> None:
            """对端已断开时不再发送关闭帧。"""
            if not disconnected:
                await websocket.close(code=code)

        try:
            connection = ProtocolConnection(runtime, protocol, bot_id, current, websocket.send_json, close_transport)
        except ValueError:
            await websocket.close(code=1008)
            return
        try:
            connection.start()
        except DuplicateBotError:
            await websocket.close(code=1008)
            return
        close_code = 1011
        reason = "未知原因"
        started = time.monotonic()
        try:
            await websocket.accept(subprotocol=subprotocol)
            logger.info("[OneBot] {} Bot {} 已连接", protocol.platform, bot_id)
            while True:
                connection.receive(await websocket.receive_json())
        except WebSocketDisconnect as error:
            disconnected = True
            close_code = error.code
            reason = "客户端断开"
        except ValueError:
            logger.warning("OneBot 连接协议无效")
            close_code = 1008
            reason = "协议无效"
        finally:
            await connection.close(close_code)
            logger.info(
                "[OneBot] {} Bot {} 已断开（{}，关闭码 {}，连接时长 {}）",
                protocol.platform,
                bot_id,
                reason,
                close_code,
                _elapsed(time.monotonic() - started),
            )

    mount_console(
        app,
        (
            auth.public_router,
            auth.router,
            bots.router,
            logs.router,
            config.router,
            adapters.router,
            membership.router,
            permissions.router,
            ai_providers.router,
            personas.router,
            chat.router,
            chat.image_router,
            amemorix.router,
            amemorix.file_router,
            local_chat.router,
            cache.router,
            stats.router,
            meta.router,
        ),
    )
    return app
