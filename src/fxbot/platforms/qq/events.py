"""QQ 官方两种传输共用的事件流，不处理 Webhook 或网关协议。"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger

from fxbot.core.errors import BotOffline, ActionFailed

from .codec import identifier, parse_event
from ...core import UnsupportedCapability


class QQEventStream:
    """每条事件独立启动任务；不做队列等待和并发上限。"""

    def __init__(self, application, client, *, bot_user_id: str):
        self.application, self.client = application, client
        self.bot_user_id = bot_user_id
        self.closed = False
        self._started = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def started(self) -> bool:
        return self._started

    def start(self, close):
        if self.closed or self._started:
            raise RuntimeError("QQ 事件流不能重复启动")
        self.application.register(self.client, close=close)
        self._started = True
        logger.info("[QQ] Bot {} 已连接", self.client.bot_id)

    def receive(self, payload: dict):
        if self.closed or not self._started:
            raise BotOffline("QQ 事件流未运行")
        try:
            event = parse_event(payload, bot_id=self.client.bot_id, bot_user_id=self.bot_user_id)
        except UnsupportedCapability:
            return
        task = self.application.spawn(self._run(payload, event), name=f"qq:{self.client.bot_id}:event")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, payload: dict, event) -> None:
        """补全频道 Bot 身份后分发，不增加原版之外的回执和去重。"""
        if event.target.kind == "channel" or event.target.target_id.startswith("dms/"):
            try:
                if not self.bot_user_id:
                    profile = await self.client.api.request("GET", "/users/@me")
                    self.bot_user_id = identifier(profile.get("id"))
                event = parse_event(payload, bot_id=self.client.bot_id, bot_user_id=self.bot_user_id)
            except (ActionFailed, httpx.HTTPError, ValueError):
                logger.exception("QQ 频道 Bot 身份查询失败，本次事件未分发")
                return
        await self.application.router.dispatch(self.client, event)

    def disconnect(self):
        """临时断线只注销在线身份，不取消已接收的事件。"""
        self._started = False
        self.application.unregister(self.client)

    async def close(self):
        self.closed = True
        self.disconnect()
        tasks = tuple(self._tasks)
        for task in tasks:
            self.application.cancel(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
