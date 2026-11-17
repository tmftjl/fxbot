"""QQ 官方接入装配：每个 AppID 明确选择一种传输。"""

from __future__ import annotations

import json
import asyncio
from contextlib import AsyncExitStack

from fastapi import Request, HTTPException
from starlette.requests import ClientDisconnect

from .gateway import QQGateway
from .signing import SignatureRejected
from .webhook import MAX_BODY_BYTES, QQWebhookConnection
from ...core.application import ApplicationState


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
        if not isinstance(connection, QQWebhookConnection):
            raise HTTPException(403, "此 QQ AppID 使用 WebSocket，不接受 Webhook")
        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise HTTPException(413, "QQ 回调数据过大")
        except ClientDisconnect:
            raise HTTPException(400, "QQ 回调连接中断") from None
        # 握手帧本身不带事件签名；其余帧验证原始字节，不能重新序列化后验签。
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or type(payload.get("op")) is not int:
                raise ValueError("QQ 回调缺少 op")
            if payload["op"] == 13:
                return connection.signer.challenge(payload.get("d"))
            connection.signer.verify(
                bytes(body),
                request.headers.get("x-signature-timestamp", ""),
                request.headers.get("x-signature-ed25519", ""),
            )
            if payload["op"] != 0:
                raise ValueError("QQ Webhook 只接受握手或 Dispatch")
            connection.receive(payload)
        except SignatureRejected:
            raise HTTPException(403, "QQ 回调签名无效或已过期") from None
        except (ValueError, TypeError, UnicodeDecodeError):
            raise HTTPException(400, "QQ 回调格式无效") from None
        return {"op": 12, "d": 0}
