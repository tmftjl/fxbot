"""全双工协议连接的事件队列和传输资源所有权。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol
from contextlib import AsyncExitStack
from collections.abc import Callable, Awaitable

from fxbot.core import Event, BotClient, UnsupportedCapability
from fxbot.core.errors import BotOffline
from fxbot.core.application import Application, ApplicationState
from fxbot.core.concurrency import run_concurrently

from .actions import ActionBroker
from .settings import Settings

logger = logging.getLogger(__name__)


class WireProtocol(Protocol):
    """平台协议只负责客户端构造及线格式编解码。"""

    platform: str

    def create_client(self, bot_id: str, sender: Callable[[str, dict], Awaitable[Any]]) -> BotClient:
        """创建当前平台客户端。"""

    def is_event(self, payload: dict) -> bool:
        """区分事件和动作响应。"""

    def parse_event(self, payload: dict, bot_id: str) -> Event | None:
        """解析事件，元事件不进入业务路由。"""

    def encode_action(self, packet: dict, bot_id: str) -> dict:
        """补齐动作帧中的平台身份。"""


class ProtocolConnection:
    """组合动作请求代理与有界并发事件消费者，关闭操作只有一个所有者。"""

    def __init__(
        self,
        application: Application,
        protocol: WireProtocol,
        bot_id: str,
        settings: Settings,
        send: Callable[[dict], Awaitable[None]],
        close_transport: Callable[[int], Awaitable[None]],
    ) -> None:
        """绑定明确的双向传输接口和连接身份。"""
        self.application = application
        self.protocol = protocol
        self._settings = settings
        self._close_transport = close_transport

        async def send_action(packet: dict) -> None:
            """动作代理不承担平台线格式转换。"""
            await send(protocol.encode_action(packet, bot_id))

        self.actions = ActionBroker(send_action, timeout=settings.action_timeout, capacity=settings.max_pending)
        self.client = protocol.create_client(bot_id, self.call)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=settings.queue_size)
        self._worker: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None

    def start(self) -> None:
        """登记连接并启动事件消费者。"""
        if self._worker is not None or self._closing is not None:
            raise RuntimeError("连接不能重复启动")
        self.application.register(self.client, close=self.close)
        self._worker = self.application.spawn(
            run_concurrently(self._consume() for _ in range(self._settings.event_concurrency)),
            name=f"{self.client.platform}:{self.client.bot_id}",
        )

    async def call(self, action: str, params: dict[str, Any]) -> Any:
        """仅允许运行中的已登记连接提交动作。"""
        if (
            self._worker is None
            or self._worker.done()
            or self._closing is not None
            or self.application.state is not ApplicationState.RUNNING
        ):
            raise BotOffline("Bot 离线")
        return await self.actions.call(action, params)

    def receive(self, payload: Any) -> None:
        """只在协议输入边界处理消息格式及尚未支持的能力。"""
        if not isinstance(payload, dict):
            raise ValueError("协议帧必须为对象")
        if not self.protocol.is_event(payload):
            self.actions.receive(payload)
            return
        try:
            event = self.protocol.parse_event(payload, self.client.bot_id)
        except UnsupportedCapability:
            logger.debug("忽略尚未支持的平台事件: %s", self.protocol.platform)
            return
        if event is not None:
            self._queue.put_nowait(event)

    async def _consume(self) -> None:
        """每个消费者只处理一条在途事件，动作响应不占业务执行槽位。"""
        while True:
            event = await self._queue.get()
            try:
                await self.application.router.dispatch(self.client, event)
            finally:
                self._queue.task_done()

    async def close(self, code: int = 1001) -> None:
        """并发关闭共享一次资源回收，取消等待者不影响关闭本身。"""
        if self._closing is None:
            self.application.retire(self.client)
            self._closing = asyncio.create_task(self._close(code), name=f"{self.client.platform}.close")
        await asyncio.shield(self._closing)

    async def _close_socket(self, code: int) -> None:
        """网络关闭有确定超时，失败交给调用边界报告。"""
        await asyncio.wait_for(self._close_transport(code), timeout=self._settings.close_timeout)

    async def _stop_consumer(self) -> None:
        """停止消费者并丢弃尚未处理的队列内容。"""
        if self._worker is not None:
            self.application.cancel(self._worker)
            await asyncio.gather(self._worker, return_exceptions=True)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    async def _close(self, code: int) -> None:
        """依次关闭动作、消费者及物理连接，最后注销身份。"""
        try:
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(self._close_socket, code)
                cleanup.push_async_callback(self._stop_consumer)
                cleanup.push_async_callback(self.actions.aclose)
        finally:
            self.application.unregister(self.client)
