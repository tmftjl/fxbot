"""应用生命周期、连接注册和受管后台任务。"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum, auto
from typing import Any
from contextlib import AsyncExitStack
from dataclasses import dataclass
from collections.abc import Callable, Awaitable, Coroutine

from .types import Message, BotClient, SessionTarget
from .router import Router

logger = logging.getLogger(__name__)
Hook = Callable[[], Awaitable[None]]


class ApplicationState(Enum):
    """应用只允许从新建经过启动和运行进入终止。"""

    NEW = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()


class DuplicateBotError(ValueError):
    """同一平台身份已有在线连接。"""


@dataclass(frozen=True, slots=True)
class _Connection:
    """绑定客户端及其唯一资源释放入口。"""

    bot: BotClient
    close: Hook


class Application:
    """通过资源栈管理单个应用实例，不使用全局宿主。"""

    def __init__(self, router: Router | None = None) -> None:
        """建立实例私有的生命周期、任务和连接注册表。"""
        self.router = router if router is not None else Router()
        self._state = ApplicationState.NEW
        self._lock = asyncio.Lock()
        self._starters: list[tuple[Hook, Hook]] = []
        self._resources = AsyncExitStack()
        self._tasks: set[asyncio.Task] = set()
        self._cancellation_requests: set[asyncio.Task] = set()
        self._message_transforms: list[Callable[[BotClient, SessionTarget, Message], Message]] = []
        self._connections: dict[tuple[str, str], _Connection] = {}
        self._retiring_connections: dict[int, _Connection] = {}
        self.plugin_metadata: dict[str, Any] = {}
        self.command_metadata: dict[str, dict[str, str]] = {}
        self.plugin_extensions: dict[str, dict[str, Callable[[], object]]] = {}
        self._shutdown: asyncio.Task | None = None

    @property
    def state(self) -> ApplicationState:
        """只读暴露生命周期状态。"""
        return self._state

    @property
    def bots(self) -> tuple[BotClient, ...]:
        """返回在线客户端快照。"""
        return tuple(connection.bot for connection in self._connections.values())

    def resource(self, start: Hook, stop: Hook) -> None:
        """在启动前注册成对的资源获取与释放函数。"""
        if self.state is not ApplicationState.NEW:
            raise RuntimeError("资源只能在启动前注册")
        self._starters.append((start, stop))

    async def start(self) -> None:
        """按序启动，部分启动失败时仍通过资源栈回收。"""
        async with self._lock:
            if self.state is ApplicationState.RUNNING:
                return
            if self.state is not ApplicationState.NEW:
                raise RuntimeError("已停止的应用不能重新启动")
            self._state = ApplicationState.STARTING
            try:
                for start, stop in self._starters:
                    self._resources.push_async_callback(stop)
                    await start()
                self._state = ApplicationState.RUNNING
            finally:
                if self.state is ApplicationState.STARTING:
                    await asyncio.shield(self._begin_close())

    def register(self, bot: BotClient, *, close: Hook) -> None:
        """登记唯一在线连接及必需的释放函数。"""
        if self.state is not ApplicationState.RUNNING:
            raise RuntimeError("应用未运行")
        key = (bot.platform, bot.bot_id)
        if key in self._connections:
            raise DuplicateBotError("Bot 已连接")
        bot.message_transform = lambda target, message: self._prepare_message(bot, target, message)
        self._connections[key] = _Connection(bot, close)

    def add_message_transform(
        self, transform: Callable[[BotClient, SessionTarget, Message], Message]
    ) -> Callable[[], None]:
        """注册原生出站消息处理，不改写平台消息类。"""
        if self.state in {ApplicationState.STOPPING, ApplicationState.STOPPED}:
            raise RuntimeError("应用已停止接收发送策略")
        if transform in self._message_transforms:
            raise ValueError("发送策略重复注册")
        self._message_transforms.append(transform)
        return lambda: self._message_transforms.remove(transform)

    def _prepare_message(self, bot: BotClient, target: SessionTarget, message: Message) -> Message:
        for transform in tuple(self._message_transforms):
            message = transform(bot, target, message)
            if not isinstance(message, Message):
                raise TypeError("发送策略必须返回原生 Message")
        return message

    def unregister(self, bot: BotClient) -> None:
        """仅注销同一客户端，旧连接不能影响新连接。"""
        key = (bot.platform, bot.bot_id)
        connection = self._connections.get(key)
        if connection is not None and connection.bot is bot:
            del self._connections[key]
        self._retiring_connections.pop(id(bot), None)

    def retire(self, bot: BotClient) -> None:
        """释放在线身份但保留关闭所有权，允许旧连接清理期间重新连接。"""
        key = (bot.platform, bot.bot_id)
        connection = self._connections.get(key)
        if connection is not None and connection.bot is bot:
            self._retiring_connections[id(bot)] = self._connections.pop(key)

    def is_registered(self, bot: BotClient) -> bool:
        """旧连接退出后，捕获它的后台任务不能误用同 ID 的新连接。"""
        connection = self._connections.get((bot.platform, bot.bot_id))
        return connection is not None and connection.bot is bot

    def get_bot(self, platform: str, bot_id: str) -> BotClient | None:
        """只查找准确的平台和账号，不跨连接回退。"""
        connection = self._connections.get((platform, bot_id))
        return connection.bot if connection is not None else None

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        """创建受管任务，拒绝启动阶段之外的新任务。"""
        if self.state not in {ApplicationState.STARTING, ApplicationState.RUNNING}:
            coroutine.close()
            raise RuntimeError("应用不接受新任务")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        """回收任务，并在后台故障的所有者边界记录异常。"""
        self._tasks.discard(task)
        self._cancellation_requests.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("后台任务失败: %s", task.get_name(), exc_info=(type(error), error, error.__traceback__))

    def cancel(self, task: asyncio.Task) -> bool:
        """受管任务只请求取消一次，等待中的异步清理不能被重复取消打断。"""
        if task.done():
            return False
        if task not in self._tasks:
            raise ValueError("任务不属于当前应用")
        if task in self._cancellation_requests:
            return False
        self._cancellation_requests.add(task)
        return task.cancel()

    async def stop(self) -> None:
        """共享一次关闭操作，取消等待者不会中断清理。"""
        async with self._lock:
            shutdown = self._begin_close()
        await asyncio.shield(shutdown)

    def _begin_close(self) -> asyncio.Task:
        """原子进入停止状态，不创建重复的关闭任务。"""
        if self._shutdown is None:
            self._state = ApplicationState.STOPPING
            self._shutdown = asyncio.create_task(self._close(), name="fxbot.shutdown")
        return self._shutdown

    async def _cancel_tasks(self) -> None:
        """取消并等待全部受管任务退出。"""
        tasks = tuple(self._tasks)
        for task in tasks:
            self.cancel(task)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._cancellation_requests.clear()

    async def _close(self) -> None:
        """按连接、任务、资源顺序释放，全部清理后向上报告失败。"""
        try:
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(self._resources.aclose)
                cleanup.push_async_callback(self._cancel_tasks)
                for connection in (*self._connections.values(), *self._retiring_connections.values()):
                    cleanup.push_async_callback(connection.close)
        finally:
            self._connections.clear()
            self._retiring_connections.clear()
            self._state = ApplicationState.STOPPED
