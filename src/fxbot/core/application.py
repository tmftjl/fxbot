"""应用生命周期、连接注册和受管后台任务。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from collections.abc import Callable, Awaitable, Coroutine

from .types import BotClient
from .router import Router

logger = logging.getLogger(__name__)
Hook = Callable[[], Awaitable[None]]


class Application:
    """管理单个应用实例的资源，不使用全局宿主或隐式启动。"""

    def __init__(self, router: Router | None = None) -> None:
        """建立实例私有状态。"""
        self.router = router if router is not None else Router()
        self.state = "new"
        self._lock = asyncio.Lock()
        self._resources: list[tuple[Hook, Hook]] = []
        self._cleanups: list[Hook] = []
        self._tasks: set[asyncio.Task] = set()
        self._bots: dict[tuple[str, str], BotClient] = {}

    @property
    def bots(self) -> tuple[BotClient, ...]:
        """返回在线客户端快照。"""
        return tuple(self._bots.values())

    def resource(self, start: Hook, stop: Hook) -> None:
        """在启动前注册资源。"""
        if self.state != "new":
            raise RuntimeError("资源只能在启动前注册")
        self._resources.append((start, stop))

    async def start(self) -> None:
        """按序启动，失败或取消时逆序回收。"""
        async with self._lock:
            if self.state == "running":
                return
            if self.state != "new":
                raise RuntimeError("已停止的应用不能重新启动")
            self.state = "starting"
            try:
                for start, stop in self._resources:
                    self._cleanups.append(stop)
                    await start()
                self.state = "running"
            except BaseException:
                await self._close()
                raise

    def register(self, bot: BotClient) -> None:
        """登记唯一在线连接。"""
        if self.state != "running":
            raise RuntimeError("应用未运行")
        key = (bot.platform, bot.bot_id)
        if key in self._bots:
            raise ValueError("Bot 已连接")
        self._bots[key] = bot

    def unregister(self, bot: BotClient) -> None:
        """仅注销同一对象。"""
        key = (bot.platform, bot.bot_id)
        if self._bots.get(key) is bot:
            del self._bots[key]

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        """创建受管后台任务。"""
        if self.state not in {"starting", "running"}:
            coroutine.close()
            raise RuntimeError("应用不接受新任务")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        """回收任务并记录后台故障。"""
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("后台任务失败: %s", task.get_name(), exc_info=(type(error), error, error.__traceback__))

    async def stop(self) -> None:
        """串行关闭且可重复调用。"""
        async with self._lock:
            if self.state == "stopped":
                return
            await self._close()

    async def _close(self) -> None:
        """先拒绝新工作，再取消任务并逆序释放资源。"""
        self.state = "stopping"
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._bots.clear()
        while self._cleanups:
            try:
                await self._cleanups.pop()()
            except Exception:
                logger.exception("资源关闭失败")
        self.state = "stopped"
