"""QQ 官方两种传输共用的事件队列，不处理 Webhook 或网关协议。"""

from __future__ import annotations

import time
import asyncio
import logging
from collections import OrderedDict

import httpx

from fxbot.core.errors import BotOffline, ActionFailed

from .codec import identifier, parse_event
from ...core import UnsupportedCapability
from ...core.concurrency import run_concurrently

logger = logging.getLogger(__name__)


class QQEventOverflow(asyncio.QueueFull):
    pass


class QQEventStream:
    def __init__(self, application, client, *, bot_user_id: str, capacity: int, concurrency: int, seen=None):
        self.application, self.client = application, client
        self.bot_user_id = bot_user_id
        self.queue = asyncio.Queue(maxsize=capacity)
        self.worker = None
        self.seen = seen if seen is not None else OrderedDict()
        self.closed = False
        self.concurrency = concurrency

    def start(self, close):
        if self.closed or self.worker is not None:
            raise RuntimeError("QQ 事件流不能重复启动")
        self.application.register(self.client, close=close)
        self.worker = self.application.spawn(
            run_concurrently(self._consume() for _ in range(self.concurrency)), name=f"qq:{self.client.bot_id}"
        )

    def receive(self, payload: dict, processed=None):
        if self.closed or self.worker is None or self.worker.done():
            raise BotOffline("QQ 事件流未运行")
        try:
            event = parse_event(payload, bot_id=self.client.bot_id, bot_user_id=self.bot_user_id)
        except UnsupportedCapability:
            if processed is not None:
                processed()
            return
        now = time.monotonic()
        while self.seen and next(iter(self.seen.values())) <= now:
            self.seen.popitem(last=False)
        key = (event.target.key(), event.metadata["qq_event_type"], event.message_id)
        if key in self.seen:
            if processed is not None:
                processed()
            return
        try:
            self.queue.put_nowait((payload, event, key, processed))
        except asyncio.QueueFull:
            raise QQEventOverflow from None
        self.seen[key] = now + 600
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)

    async def _consume(self):
        while True:
            payload, event, key, processed = await self.queue.get()
            try:
                if event.target.kind == "channel" or event.target.target_id.startswith("dms/"):
                    try:
                        if not self.bot_user_id:
                            profile = await self.client.api.request("GET", "/users/@me")
                            self.bot_user_id = identifier(profile.get("id"))
                        event = parse_event(payload, bot_id=self.client.bot_id, bot_user_id=self.bot_user_id)
                    except (ActionFailed, httpx.HTTPError, ValueError):
                        logger.exception("QQ 频道 Bot 身份查询失败，本次事件未分发")
                        continue
                await self.application.router.dispatch(self.client, event)
            finally:
                self.queue.task_done()
                # 已开始的事件不自动重做，以免重复执行发生过部分成功的写操作。
                if processed is not None:
                    processed()

    async def close(self):
        self.closed = True
        self.application.unregister(self.client)
        if self.worker is not None:
            self.application.cancel(self.worker)
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            _, _, key, _ = self.queue.get_nowait()
            # 尚未开始的事件可在 Resume 回放中再次入队。
            self.seen.pop(key, None)
            self.queue.task_done()
