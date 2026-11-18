"""按优先级分组、同组并发执行的消息路由。"""

from __future__ import annotations

from typing import Any, Unpack, TypeVar, NoReturn, TypedDict
from itertools import groupby
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from loguru import logger

from .types import Event, Message, BotClient, MessageSegment
from .context import bind_event, reset_event
from .matching import Match, MatchResult, event_match, regex_match, command_match, message_match
from .services import ServiceScope
from .contracts import ApplicationServices
from .concurrency import run_concurrently

Guard = Callable[[Event], Awaitable[bool]]
T = TypeVar("T")

_LOG_TEXT_LIMIT = 200
_LOG_SEGMENT_LIMIT = 20


def _log_value(value: Any) -> Any:
    """把消息段数据压成可读且不会膨胀日志的形式。"""
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        if len(value) <= _LOG_TEXT_LIMIT:
            return value
        return f"{value[:_LOG_TEXT_LIMIT]}…({len(value)} chars)"
    if isinstance(value, dict):
        return {key: _log_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_log_value(item) for item in value]
    return value


def _message_log_text(message: Message) -> str:
    """记录原始消息段，媒体只记类型和大小。"""
    segments = message.segments[:_LOG_SEGMENT_LIMIT]
    parts = [f"{segment.type}({_log_value(segment.data)})" for segment in segments]
    if len(message.segments) > _LOG_SEGMENT_LIMIT:
        parts.append(f"…({len(message.segments) - _LOG_SEGMENT_LIMIT} more)")
    return "[" + ", ".join(parts) + "]"


def _event_kind(event: Event) -> str:
    """还原原版事件名：QQ 用 EventType，OneBot 用 post_type.message_type.sub_type。"""
    qq_type = event.metadata.get("qq_event_type")
    if isinstance(qq_type, str) and qq_type:
        return f"EventType.{qq_type}"
    parts = [event.event_type]
    if event.event_type == "message" and event.target.kind not in parts:
        parts.append(event.target.kind)
    for key in ("post_type", "message_type", "notice_type", "sub_type"):
        value = event.metadata.get(key)
        if isinstance(value, str) and value and value not in parts:
            parts.append(value)
    return ".".join(parts)


def _event_log_text(event: Event) -> str:
    """核心日志中的用户原始信息：Bot、事件、消息 ID、用户、会话和原始消息段。"""
    sender = f"{event.user_id}({event.user_name})" if event.user_name else event.user_id
    message = event.original_message or event.message
    return (
        f"{event.platform}:{event.bot_id} [{_event_kind(event)}]: Message {event.message_id or ''} "
        f"from {sender}@[{event.target.kind}:{event.target.target_id}]: {_message_log_text(message)}"
    )


def _normalize_message_segments(event: Event) -> None:
    """文本段前置、非文本段后置，避免 @ 和图片打断命令匹配。"""
    segments = event.message.segments
    if not segments or not any(segment.type == "text" for segment in segments):
        return
    if not any(segment.type != "text" for segment in segments):
        return
    texts: list[MessageSegment] = []
    others: list[MessageSegment] = []
    first_non_empty = False
    for segment in segments:
        if segment.type != "text":
            others.append(segment)
            continue
        text = segment.text_value()
        stripped = text.strip()
        if not stripped:
            continue
        if not first_non_empty:
            first_non_empty = True
            if stripped != text:
                texts.append(MessageSegment.text(stripped))
                continue
        texts.append(segment)
    normalized = texts + others
    if normalized != segments:
        event.message.segments = normalized


class _Finished(Exception):
    """表示处理器正常提前结束，不代表停止后续路由。"""


@dataclass(slots=True, frozen=True)
class HandlerContext:
    """当前处理器显式使用的事件、客户端和命令参数。"""

    bot: BotClient
    event: Event
    match: MatchResult
    scope: ServiceScope = field(default_factory=ServiceScope)
    app_services: ApplicationServices | None = None
    state: dict[str, object] = field(default_factory=dict)

    @property
    def arguments(self) -> str:
        return self.match.arguments

    def service(self, factory: Callable[[ApplicationServices], T]) -> T:
        if self.app_services is None:
            raise RuntimeError("当前处理器没有应用服务上下文")
        return self.scope.resolve(factory, self.app_services)

    async def send(self, message: Message | str) -> Any:
        """通过捕获的目标发送，不依赖全局当前 Bot。"""
        content = Message.text(message) if isinstance(message, str) else message
        return await self.bot.send(self.event.target, content)

    async def finish(self, message: Message | str | None = None) -> NoReturn:
        """可选发送后结束当前处理器，是否阻断由路由设置决定。"""
        if message is not None:
            await self.send(message)
        raise _Finished


Handler = Callable[[HandlerContext], Awaitable[None]]
ContextCheck = Callable[[HandlerContext], Awaitable[bool]]


class RouteOptions(TypedDict, total=False):
    permission: ContextCheck | None
    rule: ContextCheck | None
    priority: int
    block: bool
    scope: ServiceScope | None
    app_services: ApplicationServices | None


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
    permission: ContextCheck | None
    rule: ContextCheck | None
    priority: int
    block: bool
    scope: ServiceScope | None
    app_services: ApplicationServices | None


class Router:
    """优先级之间顺序推进，同级响应器并发，结果按注册顺序收集。"""

    def __init__(self) -> None:
        """建立实例私有的路由及预处理列表。"""
        self._routes: list[_Route] = []
        self._preprocessors: list[Guard] = []
        self._groups: tuple[tuple[_Route, ...], ...] = ()

    def validate_names(self, names: tuple[str, ...]) -> None:
        existing = {route.name for route in self._routes}
        if len(names) != len(set(names)) or existing.intersection(names):
            raise ValueError("路由名称必须唯一")

    def add_preprocessor(self, guard: Guard) -> None:
        """注册全局准入检查，返回 False 或异常均停止事件处理。"""
        self._preprocessors.append(guard)

    def add(
        self,
        name: str,
        handler: Handler,
        *,
        match: Match,
        permission: ContextCheck | None = None,
        rule: ContextCheck | None = None,
        priority: int = 5,
        block: bool = True,
        scope: ServiceScope | None = None,
        app_services: ApplicationServices | None = None,
    ) -> None:
        """注册显式匹配函数，无权限函数表示公开路由。"""
        if not isinstance(name, str) or not name or any(route.name == name for route in self._routes):
            raise ValueError("路由名称必须非空且唯一")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("路由优先级必须为整数")
        if not isinstance(block, bool):
            raise ValueError("路由阻断设置必须为布尔值")
        self._routes.append(_Route(name, handler, match, permission, rule, priority, block, scope, app_services))
        self._groups = tuple(
            tuple(group)
            for _, group in groupby(
                sorted(self._routes, key=lambda route: route.priority), key=lambda route: route.priority
            )
        )

    def command(self, name: str, command: str, handler: Handler, **options: Unpack[RouteOptions]) -> None:
        self.add(name, handler, match=command_match(command, prefixes=("",)), **options)

    def message(self, name: str, handler: Handler, **options: Unpack[RouteOptions]) -> None:
        self.add(name, handler, match=message_match, **options)

    def regex(self, name: str, pattern: str, handler: Handler, **options: Unpack[RouteOptions]) -> None:
        self.add(name, handler, match=regex_match(pattern, strip=False), **options)

    def event(self, name: str, event_type: str, handler: Handler, **options: Unpack[RouteOptions]) -> None:
        self.add(name, handler, match=event_match(event_type), **options)

    async def dispatch(self, bot: BotClient, event: Event) -> DispatchResult:
        """分发单条事件，异常闭锁、取消传播，并在全部退出路径恢复上下文。"""
        logger.info("{}", _event_log_text(event))
        _normalize_message_segments(event)
        token = bind_event(bot, event)
        handled: list[str] = []
        denied: list[str] = []
        failed: list[str] = []
        groups = self._groups
        preprocessors = tuple(self._preprocessors)
        try:
            for guard in preprocessors:
                try:
                    if await guard(event) is not True:
                        return DispatchResult(blocked=True, rejected=True)
                except Exception:
                    logger.exception("事件准入检查失败: {}", _event_log_text(event))
                    return DispatchResult(failed=("preprocessor",), blocked=True, rejected=True)
            for group in groups:
                results = await run_concurrently(self._dispatch_route(route, bot, event) for route in group)
                for result in results:
                    handled.extend(result.handled)
                    denied.extend(result.denied)
                    failed.extend(result.failed)
                if any(result.blocked for result in results):
                    return DispatchResult(tuple(handled), tuple(denied), tuple(failed), blocked=True)
            return DispatchResult(tuple(handled), tuple(denied))
        finally:
            reset_event(token)

    async def _dispatch_route(self, route: _Route, bot: BotClient, event: Event) -> DispatchResult:
        handled: tuple[str, ...] = ()
        try:
            arguments = route.match(event)
            if arguments is None:
                return DispatchResult()
            if not isinstance(arguments, MatchResult):
                raise TypeError("匹配器必须返回 MatchResult 或 None")
            context = HandlerContext(
                bot,
                event,
                arguments,
                route.scope if route.scope is not None else ServiceScope(),
                route.app_services,
            )
            if route.permission is not None and await route.permission(context) is not True:
                return DispatchResult(denied=(route.name,))
            if route.rule is not None and await route.rule(context) is not True:
                return DispatchResult()
            handled = (route.name,)
            logger.success("事件将由 {} 处理", route.name)
            try:
                await route.handler(context)
            except _Finished:
                pass
            return DispatchResult(handled=handled, blocked=route.block)
        except Exception:
            logger.exception("路由执行失败: {} {}", route.name, _event_log_text(event))
            return DispatchResult(handled=handled, failed=(route.name,), blocked=True)
