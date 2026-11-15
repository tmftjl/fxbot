"""不可变响应器声明与单次执行作用域。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, NoReturn
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from .rule import Rule
from .types import Event, Message
from .router import HandlerContext
from .binding import HandlerBinding
from .matching import Match, MatchResult
from .services import ServiceScope
from .contracts import ApplicationServices
from .permission import Permission

if TYPE_CHECKING:
    from .application import Application

H = TypeVar("H", bound=Callable[..., Awaitable[None]])
Authorize = Callable[[str, Event], Awaitable[bool]]


@dataclass(frozen=True, slots=True, kw_only=True)
class MatcherOptions:
    name: str = ""
    display_name: str = ""
    level: str = "member"
    scene: str = "all"
    priority: int = 5
    block: bool = True
    rule: Rule = Rule()
    permission: Permission = Permission()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or ":" in self.name:
            raise ValueError("响应器名称必须是不含冒号的字符串")
        if self.scene not in {"all", "group", "private"}:
            raise ValueError("未知命令场景")
        if self.level not in {"all", "member", "admin", "owner", "bot_admin", "superuser"}:
            raise ValueError("未知权限等级")
        if type(self.priority) is not int or type(self.block) is not bool:
            raise TypeError("优先级必须为整数，阻断设置必须为布尔值")


@dataclass(slots=True)
class _Execution:
    matcher: Matcher
    context: HandlerContext
    active: bool = True


_current: ContextVar[_Execution | None] = ContextVar("fxbot_matcher", default=None)


class Matcher:
    """模块只持有定义；所有可变状态属于正在执行的调用。"""

    __slots__ = ("_options", "_match", "_handlers", "_sealed")

    def __init__(self, match: Match, options: MatcherOptions) -> None:
        self._options = options
        self._match = match
        self._handlers: list[HandlerBinding] = []
        self._sealed = False

    @property
    def options(self) -> MatcherOptions:
        return self._options

    @property
    def match(self) -> Match:
        return self._match

    def handle(self) -> Callable[[H], H]:
        def decorate(handler: H) -> H:
            if self._sealed:
                raise RuntimeError("已加载的响应器不能增加处理器")
            if any(item.callback is handler for item in self._handlers):
                raise ValueError("处理器不能重复注册")
            self._handlers.append(HandlerBinding.compile(handler, Matcher))
            return handler

        return decorate

    def snapshot(self) -> MatcherDefinition:
        if not self._handlers:
            raise ValueError("响应器至少需要一个处理器")
        self._sealed = True
        options = self.options
        return MatcherDefinition(
            matcher=self,
            name=options.name or self._handlers[0].callback.__name__,
            display_name=options.display_name or options.name or self._handlers[0].callback.__name__,
            level=options.level,
            scene=options.scene,
            priority=options.priority,
            block=options.block,
            match=self.match,
            rule=options.rule,
            permission=options.permission,
            handlers=tuple(self._handlers),
        )

    def _context(self) -> HandlerContext:
        current = _current.get()
        if current is None or not current.active or current.matcher is not self:
            raise RuntimeError("响应器当前没有执行上下文")
        return current.context

    async def send(self, message: Message | str) -> Any:
        return await self._context().send(message)

    async def finish(self, message: Message | str | None = None) -> NoReturn:
        await self._context().finish(message)


@dataclass(frozen=True, slots=True)
class MatcherDefinition:
    matcher: Matcher
    name: str
    display_name: str
    level: str
    scene: str
    priority: int
    block: bool
    match: Match
    rule: Rule
    permission: Permission
    handlers: tuple[HandlerBinding, ...]

    def matches(self, event: Event) -> MatchResult | None:
        if self.scene != "all" and event.target.kind != self.scene:
            return None
        return self.match(event)

    async def run(self, context: HandlerContext) -> None:
        execution = _Execution(self.matcher, context)
        token = _current.set(execution)
        try:
            for handler in self.handlers:
                await handler.invoke(context, self.matcher)
        finally:
            execution.active = False
            _current.reset(token)


@dataclass(frozen=True, slots=True)
class _BoundPermission:
    feature: str
    custom: Permission
    authorize: Authorize | None

    async def __call__(self, context: HandlerContext) -> bool:
        if self.authorize is not None and await self.authorize(self.feature, context.event) is not True:
            return False
        return await self.custom(context)


def bind_matchers(
    application: Application,
    namespace: str,
    definitions: tuple[MatcherDefinition, ...],
    *,
    authorize: Authorize | None = None,
    scope: ServiceScope | None = None,
    app_services: ApplicationServices | None = None,
) -> None:
    """统一装配核心业务与目录插件的声明；Router 仅负责执行。"""
    names = tuple(f"{namespace}:{item.name}" for item in definitions)
    application.router.validate_names(names)
    for item, name in zip(definitions, names, strict=True):
        application.router.add(
            name,
            item.run,
            match=item.matches,
            rule=item.rule,
            permission=_BoundPermission(name, item.permission, authorize),
            priority=item.priority,
            block=item.block,
            scope=scope,
            app_services=app_services,
        )
    application.command_metadata.setdefault(namespace, {}).update(
        {item.name: item.display_name for item in definitions}
    )
