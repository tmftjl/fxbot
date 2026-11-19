"""QQ 官方已验签事件的分发与应用资源所有权。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import httpx
from fastapi import HTTPException

from fxbot.core.errors import BotOffline

from .api import QQHTTP
from .client import QQClient
from .events import QQEventStream
from .signing import QQSigner
from ...core.application import ApplicationState, DuplicateBotError


class QQWebhookConnection:
    def __init__(self, application, app_id, config, settings):
        self.application = application
        self._closed = False
        self.app_id, self.config = app_id, config
        self.signer = QQSigner(config.secret)
        self.api = QQHTTP(
            app_id,
            config.secret,
            httpx.AsyncClient(timeout=settings.action_timeout, follow_redirects=False, trust_env=False),
            api_base=config.api_base,
            auth_url=config.auth_url,
        )
        self.client: QQClient = QQClient(
            app_id, self.api, available=lambda: not self._closed and application.is_registered(self.client)
        )
        self.events = QQEventStream(application, self.client, bot_user_id=config.bot_user_id)
        self._close_task = None

    def receive(self, payload: dict) -> None:
        if self._closed or self.application.state is not ApplicationState.RUNNING:
            raise HTTPException(503, "QQ Bot 未就绪")
        if not self.events.started:
            try:
                self.events.start(self.close)
            except DuplicateBotError:
                raise HTTPException(409, "QQ Bot 已由另一连接占用") from None
        try:
            self.events.receive(payload)
        except BotOffline:
            raise HTTPException(503, "QQ 事件流已停止") from None

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close(), name=f"qq:{self.app_id}:close")
        await asyncio.shield(self._close_task)

    async def _close(self):
        async with AsyncExitStack() as cleanup:
            cleanup.callback(self.client._sequence.clear)
            cleanup.callback(self.client._sent.clear)
            cleanup.push_async_callback(self.api.close)
            cleanup.push_async_callback(self.events.close)
