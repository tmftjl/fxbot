"""独立 ASGI 应用、健康检查与 OneBot 连接端点。"""

from __future__ import annotations

import hmac
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from fxbot.core.application import Application

from .onebot11 import OneBotConnection
from .settings import Settings
from ..bootstrap import create_application

logger = logging.getLogger(__name__)


def create_app(settings: Settings, application: Application | None = None) -> FastAPI:
    """创建独立实例，不加载旧应用、数据库或业务插件。"""
    runtime = application if application is not None else create_application()
    connections: set[OneBotConnection] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动应用并回收全部连接和后台任务。"""
        await runtime.start()
        try:
            yield
        finally:
            for connection in tuple(connections):
                await connection.close()
            await runtime.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime

    @app.get("/health")
    async def health() -> dict[str, bool]:
        """仅返回实例是否就绪。"""
        return {"ok": runtime.state == "running"}

    @app.websocket("/onebot/v11/ws")
    async def onebot(websocket: WebSocket) -> None:
        """只接受通过令牌和身份校验的全双工连接。"""
        authorization = websocket.headers.get("authorization", "")
        bot_id = websocket.headers.get("x-self-id", "")
        if (
            not hmac.compare_digest(authorization.encode(), f"Bearer {settings.token}".encode())
            or bot_id not in settings.bot_ids
            or websocket.headers.get("x-client-role", "").lower() != "universal"
            or runtime.state != "running"
        ):
            await websocket.close(code=1008)
            return
        connection = OneBotConnection(runtime, bot_id, settings, websocket.send_json)
        try:
            connection.start()
        except ValueError:
            await websocket.close(code=1008)
            return
        connections.add(connection)
        try:
            await websocket.accept()
            while True:
                connection.receive(await websocket.receive_json())
        except WebSocketDisconnect:
            pass
        except (ValueError, asyncio.QueueFull):
            logger.warning("OneBot 连接协议无效或事件队列过载")
            await websocket.close(code=1008)
        except Exception:
            logger.exception("OneBot 连接处理失败")
            await websocket.close(code=1011)
        finally:
            connections.discard(connection)
            await connection.close()

    return app
