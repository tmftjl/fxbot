"""在注册时编译处理器参数；执行时只读取已确定的数据源。"""

from __future__ import annotations

import inspect
from typing import get_type_hints
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from .types import Event, BotClient
from .router import HandlerContext
from .matching import MatchResult
from .contracts import ApplicationServices

State = dict[str, object]


class CommandArguments(str):
    """当前命令参数，与普通业务字符串参数明确区分。"""


@dataclass(frozen=True, slots=True)
class ParameterBinding:
    name: str
    positional: bool
    source: str

    def value(self, context: HandlerContext, matcher: object) -> object:
        if self.source == "context":
            return context
        if self.source == "matcher":
            return matcher
        if self.source == "arguments":
            return CommandArguments(context.arguments)
        if self.source == "services":
            if context.app_services is None:
                raise RuntimeError("当前处理器没有应用服务")
            return context.app_services
        return getattr(context, self.source)


@dataclass(frozen=True, slots=True)
class HandlerBinding:
    callback: Callable[..., Awaitable[None]]
    parameters: tuple[ParameterBinding, ...]

    @classmethod
    def compile(cls, callback: Callable[..., Awaitable[None]], matcher_type: type) -> HandlerBinding:
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("响应器处理器必须是异步函数")
        parameters = tuple(inspect.signature(callback).parameters.values())
        hints = get_type_hints(callback)
        sources = {
            HandlerContext: "context",
            Event: "event",
            BotClient: "bot",
            State: "state",
            MatchResult: "match",
            ApplicationServices: "services",
            CommandArguments: "arguments",
            matcher_type: "matcher",
        }
        bindings = []
        for parameter in parameters:
            if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
                raise TypeError("处理器不接受可变参数")
            annotation = hints.get(parameter.name, parameter.annotation)
            if annotation is inspect.Parameter.empty and len(parameters) == 1:
                source: str | None = "context"
            else:
                source = sources.get(annotation)
            if source is None:
                raise TypeError(f"处理器参数 {parameter.name} 必须声明受支持的上下文类型")
            bindings.append(
                ParameterBinding(parameter.name, parameter.kind is inspect.Parameter.POSITIONAL_ONLY, source)
            )
        return cls(callback, tuple(bindings))

    async def invoke(self, context: HandlerContext, matcher: object) -> None:
        args, kwargs = [], {}
        for parameter in self.parameters:
            value = parameter.value(context, matcher)
            if parameter.positional:
                args.append(value)
            else:
                kwargs[parameter.name] = value
        await self.callback(*args, **kwargs)
