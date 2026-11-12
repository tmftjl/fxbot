"""显式注册、顺序执行的消息路由。"""

from __future__ import annotations

import logging
from typing import Any
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from .types import Event, Message, BotClient
from .context import bind_event, reset_event

logger = logging.getLogger(__name__)
Guard = Callable[[Event], Awaitable[bool]]


class _Finished(Exception):
    """表示处理器正常提前结束，不代表停止后续路由。"""


@dataclass(slots=True, frozen=True)
class HandlerContext:
    """当前处理器显式使用的事件、客户端和命令参数。"""

    bot: BotClient
    event: Event
    arguments: str

    async def send(self, message: Message | str) -> Any:
        """通过捕获的目标发送，不依赖全局当前 Bot。"""
        content = Message.text(message) if isinstance(message, str) else message
        return await self.bot.send(self.event.target, content)

    async def finish(self, message: Message | str | None = None) -> None:
        """可选发送后结束当前处理器，是否阻断由路由设置决定。"""
        if message is not None:
            await self.send(message)
        raise _Finished


Handler = Callable[[HandlerContext], Awaitable[None]]
Match = Callable[[Event], str | None]


@dataclass(slots=True, frozen=True)
class DispatchResult:
    """只记录内部路由结果，不包含异常内容或用户回复。"""

    handled: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    blocked: bool = False
    rejected: bool = False


@dataclass(slots=True, frozen=True)
class _Route:
    """一项按注册顺序参与分发的路由。"""

    name: str
    handler: Handler
    match: Match
    permission: Guard | None
    rule: Guard | None
    priority: int
    block: bool


class Router:
    """按优先级和注册顺序执行消息处理器，同级不并发。"""

    def __init__(self) -> None:
        """建立实例私有的路由及预处理列表。"""
        self._routes: list[_Route] = []
        self._preprocessors: list[Guard] = []

    def add_preprocessor(self, guard: Guard) -> None:
        """注册全局准入检查，返回 False 或异常均停止事件处理。"""
        self._preprocessors.append(guard)

    def add(
        self,
        name: str,
        handler: Handler,
        *,
        match: Match,
        permission: Guard | None = None,
        rule: Guard | None = None,
        priority: int = 5,
        block: bool = True,
    ) -> None:
        """注册显式匹配函数，无权限函数表示公开路由。"""
        if not isinstance(name, str) or not name or any(route.name == name for route in self._routes):
            raise ValueError("路由名称必须非空且唯一")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("路由优先级必须为整数")
        if not isinstance(block, bool):
            raise ValueError("路由阻断设置必须为布尔值")
        self._routes.append(_Route(name, handler, match, permission, rule, priority, block))

    def command(self, name: str, command: str, handler: Handler, **options: Any) -> None:
        """注册大小写敏感的完整命令词，参数必须由空白分隔，前缀需显式给出。"""
        if not isinstance(command, str) or not command or any(char.isspace() for char in command):
            raise ValueError("命令词必须非空且不含空白")

        def match(event: Event) -> str | None:
            """返回命令参数，未命中返回 None。"""
            if not event.is_message:
                return None
            words = event.message.plain_text().strip().split(maxsplit=1)
            if not words or words[0] != command:
                return None
            return words[1] if len(words) == 2 else ""

        self.add(name, handler, match=match, **options)

    def message(self, name: str, handler: Handler, **options: Any) -> None:
        """注册通用消息处理，规则通过后将文本作为参数。"""
        self.add(
            name,
            handler,
            match=lambda event: event.message.plain_text() if event.is_message else None,
            **options,
        )

    async def dispatch(self, bot: BotClient, event: Event) -> DispatchResult:
        """分发单条事件，异常闭锁、取消传播，并在全部退出路径恢复上下文。"""
        token = bind_event(bot, event)
        handled: list[str] = []
        denied: list[str] = []
        routes = sorted(self._routes, key=lambda route: route.priority)
        preprocessors = tuple(self._preprocessors)
        try:
            for guard in preprocessors:
                try:
                    if await guard(event) is not True:
                        return DispatchResult(blocked=True, rejected=True)
                except Exception:
                    logger.exception("事件准入检查失败")
                    return DispatchResult(failed=("preprocessor",), blocked=True, rejected=True)
            for route in routes:
                try:
                    arguments = route.match(event)
                    if arguments is None or (route.rule is not None and await route.rule(event) is not True):
                        continue
                    if route.permission is not None and await route.permission(event) is not True:
                        denied.append(route.name)
                        continue
                    handled.append(route.name)
                    try:
                        await route.handler(HandlerContext(bot, event, arguments))
                    except _Finished:
                        pass
                except Exception:
                    logger.exception("路由执行失败: %s", route.name)
                    return DispatchResult(tuple(handled), tuple(denied), (route.name,), blocked=True)
                if route.block:
                    return DispatchResult(tuple(handled), tuple(denied), blocked=True)
            return DispatchResult(tuple(handled), tuple(denied))
        finally:
            reset_event(token)
