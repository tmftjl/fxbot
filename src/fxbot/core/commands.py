"""模块级命令、消息和事件声明。"""

from __future__ import annotations

import re
import inspect
from typing import Protocol
from dataclasses import dataclass
from collections.abc import Callable, Iterable

from .types import Event
from .router import Guard, Handler, HandlerContext
from .application import Application


@dataclass(frozen=True, slots=True)
class Command:
    """权限声明，由核心从处理器元数据生成。"""

    name: str
    pattern: str
    handler: Handler
    level: str = "member"
    scene: str = "all"
    priority: int = 5
    block: bool = True


@dataclass(frozen=True, slots=True)
class CommandDeclaration:
    name: str = ""
    pattern: str = ""
    level: str = "member"
    scene: str = "all"
    priority: int = 5
    block: bool = True
    kind: str = "regex"
    event_type: str = ""
    when: Callable | None = None
    flags: int = 0
    display_name: str = ""


def _declare(declaration: CommandDeclaration):
    if declaration.scene not in {"all", "group", "private", "guild", "channel"}:
        raise ValueError("未知命令场景")
    if declaration.level not in {"all", "member", "admin", "owner", "bot_admin", "superuser"}:
        raise ValueError("未知权限等级")
    if type(declaration.priority) is not int or type(declaration.block) is not bool:
        raise TypeError("优先级必须为整数，阻断设置必须为布尔值")

    def decorator(handler: Handler) -> Handler:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("处理器必须是异步函数")
        if "__fxbot_command__" in vars(handler):
            raise ValueError("同一处理器不能重复声明")
        handler.__fxbot_command__ = declaration
        return handler

    return decorator


def on_regex(
    pattern: str,
    *,
    name: str = "",
    level: str = "member",
    scene: str = "all",
    priority: int = 5,
    block: bool = True,
    flags: int = 0,
    display_name: str = "",
    when=None,
):
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("正则表达式不能为空")
    re.compile(pattern, flags)
    return _declare(
        CommandDeclaration(
            name, pattern, level, scene, priority, block, flags=flags, display_name=display_name, when=when
        )
    )


def on_command(
    command: str,
    *,
    aliases: Iterable[str] = (),
    name: str = "",
    level: str = "member",
    scene: str = "all",
    priority: int = 5,
    block: bool = True,
    display_name: str = "",
):
    """匹配明确前缀和完整命令词，参数由空白分隔。"""
    if isinstance(aliases, str):
        raise TypeError("aliases 必须是命令词集合")
    words = tuple(dict.fromkeys((command, *sorted(aliases))))
    if any(not isinstance(word, str) or not word or any(ch.isspace() for ch in word) for word in words):
        raise ValueError("命令词必须非空且不含空白")
    pattern = r"^[#＃/](?:" + "|".join(re.escape(word) for word in words) + r")(?:\s+(.*))?$"
    return _declare(
        CommandDeclaration(
            name or command,
            pattern,
            level,
            scene,
            priority,
            block,
            kind="command",
            display_name=display_name or command,
        )
    )


def on_event(
    event_type: str,
    *,
    name: str = "",
    level: str = "all",
    scene: str = "all",
    priority: int = 5,
    block: bool = False,
    display_name: str = "",
    when=None,
):
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("事件类型不能为空")
    return _declare(
        CommandDeclaration(
            name=name,
            level=level,
            scene=scene,
            priority=priority,
            block=block,
            kind="event",
            event_type=event_type,
            display_name=display_name,
            when=when,
        )
    )


def on_message(
    *,
    name: str = "",
    level: str = "all",
    scene: str = "all",
    priority: int = 5,
    block: bool = True,
    when=None,
    display_name: str = "",
):
    return _declare(
        CommandDeclaration(
            name=name,
            level=level,
            scene=scene,
            priority=priority,
            block=block,
            kind="message",
            when=when,
            display_name=display_name,
        )
    )


def collect_handlers(modules) -> tuple[tuple[Handler, CommandDeclaration], ...]:
    result = []
    seen = set()
    for module in modules:
        for handler in vars(module).values():
            if not inspect.isfunction(handler) or handler.__module__ != module.__name__:
                continue
            declaration = getattr(handler, "__fxbot_command__", None)
            if not isinstance(declaration, CommandDeclaration) or handler in seen:
                continue
            seen.add(handler)
            result.append((handler, declaration))
    return tuple(result)


def collect_commands(module: object) -> tuple[Command, ...]:
    return tuple(
        Command(
            declaration.name or handler.__name__,
            declaration.pattern,
            handler,
            declaration.level,
            declaration.scene,
            declaration.priority,
            declaration.block,
        )
        for handler, declaration in collect_handlers((module,))
    )


class CommandAuthorizer(Protocol):
    def guard(self, feature: str) -> Guard: ...


def register_declarations(
    application: Application, authorizer: CommandAuthorizer, feature: str, declarations, services, runtime
) -> tuple[Command, ...]:
    commands = []
    for handler, declaration in declarations:
        name = declaration.name or handler.__name__
        expression = re.compile(declaration.pattern, declaration.flags) if declaration.pattern else None

        def match(event: Event, d=declaration, expression=expression):
            if d.scene != "all" and event.target.kind != d.scene:
                return None
            if d.kind == "event":
                return "" if event.event_type == d.event_type else None
            if not event.is_message:
                return None
            text = event.message.plain_text().strip()
            if expression is None:
                return text
            matched = expression.search(text)
            if matched is None:
                return None
            return (matched.group(1) or "") if d.kind == "command" else matched.group(0)

        async def invoke(context: HandlerContext, handler=handler):
            await handler(HandlerContext(context.bot, context.event, context.arguments, services, runtime))

        async def rule(event: Event, d=declaration, match=match):
            context = HandlerContext(
                runtime.application.get_bot(event.platform, event.bot_id), event, match(event) or "", services, runtime
            )
            return await d.when(context) if d.when is not None else True

        application.router.add(
            f"{feature}:{name}",
            invoke,
            match=match,
            rule=rule if declaration.when is not None else None,
            permission=authorizer.guard(f"{feature}:{name}"),
            priority=declaration.priority,
            block=declaration.block,
        )
        commands.append(
            Command(
                name,
                declaration.pattern,
                handler,
                declaration.level,
                declaration.scene,
                declaration.priority,
                declaration.block,
            )
        )
    return tuple(commands)


def register_module_commands(application, authorizer, feature, module):
    """核心测试及独立模块注册使用同一声明规则。"""
    from types import SimpleNamespace

    return register_declarations(
        application, authorizer, feature, collect_handlers((module,)), {}, SimpleNamespace(application=application)
    )
