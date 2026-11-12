"""OneBot V11 全双工反向连接和动作响应关联。"""

from __future__ import annotations

import uuid
import asyncio
import logging
from typing import Any
from collections.abc import Callable, Awaitable

from fxbot.core import UnsupportedCapability
from fxbot.core.application import Application
from fxbot.core.platforms.onebot11 import OneBot11Client, parse_event

from .settings import Settings

logger = logging.getLogger(__name__)


class BotOffline(ConnectionError):
    """客户端已离线，不能发送动作。"""


class ActionFailed(RuntimeError):
    """平台未成功执行动作，不携带上游响应。"""


class OneBotConnection:
    """读循环与事件消费者分离，等待动作时仍能接收响应。"""

    def __init__(
        self,
        application: Application,
        bot_id: str,
        settings: Settings,
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        """绑定单条连接，重连时创建全新请求命名空间。"""
        self.application = application
        self.settings = settings
        self._send = send
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=settings.queue_size)
        self._worker: asyncio.Task | None = None
        self._closed = False
        self.client = OneBot11Client(bot_id, self.call)

    def start(self) -> None:
        """登记连接并开始顺序消费事件。"""
        if self._worker is not None or self._closed:
            raise RuntimeError("连接不能重复启动")
        self.application.register(self.client)
        try:
            self._worker = self.application.spawn(self._consume(), name=f"onebot11:{self.client.bot_id}")
        except BaseException:
            self.application.unregister(self.client)
            raise

    async def call(self, action: str, params: dict[str, Any]) -> Any:
        """在限定时长内发送动作并等待响应，不自动重试发送。"""
        if self._closed or self._worker is None or self.application.state != "running":
            raise BotOffline("Bot 离线")
        if len(self._pending) >= self.settings.max_pending:
            raise ActionFailed("待处理动作已满")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future

        async def execute() -> Any:
            """发送锁只保护写入，不阻塞动作响应处理。"""
            async with self._send_lock:
                if self._closed:
                    raise BotOffline("Bot 离线")
                await self._send({"action": action, "params": params, "echo": echo})
            return await future

        try:
            return await asyncio.wait_for(execute(), timeout=self.settings.action_timeout)
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def receive(self, payload: Any) -> None:
        """优先关联响应，事件过载时断连而非阻塞响应读取。"""
        if self._closed:
            raise BotOffline("Bot 离线")
        if not isinstance(payload, dict):
            raise ValueError("协议帧必须为对象")
        if "post_type" not in payload:
            echo = payload.get("echo")
            future = self._pending.get(echo) if isinstance(echo, str) else None
            if future is not None and not future.done():
                if payload.get("status") == "ok" and type(payload.get("retcode")) is int and payload["retcode"] == 0:
                    future.set_result(payload.get("data"))
                else:
                    future.set_exception(ActionFailed("平台动作失败"))
            return
        if payload.get("post_type") == "meta_event":
            if str(payload.get("self_id")) != self.client.bot_id:
                raise ValueError("连接身份不一致")
            return
        try:
            event = parse_event(payload, bot_id=self.client.bot_id)
        except UnsupportedCapability:
            logger.debug("忽略尚未支持的 OneBot 事件")
            return
        self._queue.put_nowait(event)

    async def _consume(self) -> None:
        """顺序分发事件，队列满时由网络边界执行断连。"""
        while True:
            event = await self._queue.get()
            try:
                await self.application.router.dispatch(self.client, event)
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        """使待处理请求失败，取消消费者并注销连接。"""
        if self._closed:
            return
        self._closed = True
        self.application.unregister(self.client)
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(BotOffline("Bot 离线"))
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
