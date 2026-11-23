"""全双工协议连接的事件分发和传输资源所有权。"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol
from contextlib import AsyncExitStack
from collections.abc import Callable, Awaitable

from loguru import logger

from fxbot.core import Event, BotClient, UnsupportedCapability
from fxbot.core.errors import BotOffline
from fxbot.core.application import Application, ApplicationState

from .actions import ActionBroker
from .settings import Settings


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
    """组合动作请求代理与逐事件任务，关闭操作只有一个所有者。"""

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
        self._tasks: set[asyncio.Task] = set()
        self._started = False
        self._closing: asyncio.Task | None = None

    def start(self) -> None:
        """登记连接；每个事件在接收时独立启动任务。"""
        if self._started or self._closing is not None:
            raise RuntimeError("连接不能重复启动")
        self.application.register(self.client, close=self.close)
        self._started = True

    async def call(self, action: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        """仅允许运行中的已登记连接提交动作，可覆盖单项动作的超时。"""
        if not self._started or self._closing is not None or self.application.state is not ApplicationState.RUNNING:
            raise BotOffline("Bot 离线")
        return await self.actions.call(action, params, timeout=timeout)

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
            logger.debug("忽略尚未支持的平台事件: {}", self.protocol.platform)
            return
        if event is None or self._closing is not None:
            return
        task = self.application.spawn(
            self.application.router.dispatch(self.client, event),
            name=f"{self.client.platform}:{self.client.bot_id}:event",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self, code: int = 1001) -> None:
        """并发关闭共享一次资源回收，取消等待者不影响关闭本身。"""
        if self._closing is None:
            self.application.retire(self.client)
            self._closing = asyncio.create_task(self._close(code), name=f"{self.client.platform}.close")
        await asyncio.shield(self._closing)

    async def _close_socket(self, code: int) -> None:
        """网络关闭有确定超时，失败交给调用边界报告。"""
        await asyncio.wait_for(self._close_transport(code), timeout=self._settings.close_timeout)

    async def _close(self, code: int) -> None:
        """只关闭本次传输；事件任务留给应用整体关闭时回收。"""
        try:
            self._started = False
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(self._close_socket, code)
                cleanup.push_async_callback(self.actions.aclose)
        finally:
            self.application.unregister(self.client)
