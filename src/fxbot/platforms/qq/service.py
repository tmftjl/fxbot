"""QQ 官方接入装配：Webhook 回调与可选的主动 WebSocket 网关。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from fastapi import Request, Response, HTTPException
from pydantic import TypeAdapter
from starlette.requests import ClientDisconnect

from .models import User, Dispatch, PayloadType, WebhookVerify
from .gateway import QQGateway
from .signing import SignatureRejected
from .webhook import QQWebhookConnection
from ...core.application import ApplicationState

_PAYLOAD = TypeAdapter(PayloadType)


class QQService:
    def __init__(self, application, settings):
        self.application, self.settings = application, settings
        self.connections = {}
        self._resources = AsyncExitStack()
        self._lock = asyncio.Lock()

    async def start(self):
        for app_id, config in self.settings.qq_bots.items():
            connection: QQGateway | QQWebhookConnection
            if config.mode == "websocket":
                connection = QQGateway(self.application, app_id, config, self.settings)
            else:
                connection = QQWebhookConnection(self.application, app_id, config, self.settings)
            self._resources.push_async_callback(connection.close)
            self.connections[app_id] = connection
            if isinstance(connection, QQGateway):
                connection.start()

    async def reload(self, settings):
        """按新凭证重建连接；Webhook 立即切换验签密钥，网关重新握手。"""
        async with self._lock:
            if self.application.state is not ApplicationState.RUNNING:
                raise RuntimeError("应用未运行，不能重载 QQ Bot 配置")
            await self._resources.aclose()
            self.connections.clear()
            self.settings = settings
            self._resources = AsyncExitStack()
            await self.start()

    async def close(self):
        async with self._lock:
            try:
                await self._resources.aclose()
            finally:
                self.connections.clear()

    async def handle(self, request: Request):
        app_id = request.headers.get("x-bot-appid", "")
        if app_id not in self.connections:
            raise HTTPException(403, "QQ AppID 未授权")
        connection = self.connections[app_id]
        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
        except ClientDisconnect:
            raise HTTPException(400, "QQ 回调连接中断") from None
        # 握手帧本身不带事件签名；其余帧验证原始字节，不能重新序列化后验签。
        try:
            frame = _PAYLOAD.validate_json(body)
        except ValueError:
            raise HTTPException(400, "QQ 回调格式无效") from None
        if isinstance(frame, WebhookVerify):
            return connection.signer.challenge(frame.data.model_dump())
        try:
            if connection.config.verify_webhook:
                connection.signer.verify(
                    bytes(body),
                    request.headers.get("x-signature-timestamp", ""),
                    request.headers.get("x-signature-ed25519", ""),
                )
        except SignatureRejected:
            raise HTTPException(403, "QQ 回调签名无效") from None
        if isinstance(connection, QQWebhookConnection):
            if not connection.events.bot_user_id:
                profile = User.model_validate(await connection.api.request("GET", "/users/@me"))
                connection.events.bot_user_id = profile.id
                connection.events.bot_username = profile.username
            if not connection.events.started:
                connection.events.start(connection.close)
        else:
            if not connection._bot_user_id:
                profile = User.model_validate(await connection.api.request("GET", "/users/@me"))
                connection._bot_user_id = profile.id
                connection._bot_username = profile.username
            connection._activate()
        if isinstance(frame, Dispatch):
            connection.receive(frame.model_dump(by_alias=True, mode="json"))
        return Response(status_code=200)
