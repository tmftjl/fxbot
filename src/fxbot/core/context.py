"""独立运行时的事件上下文，不依赖框架全局变量。"""

from __future__ import annotations

from contextvars import Token, ContextVar
from dataclasses import dataclass

from .types import Event, BotClient


@dataclass(slots=True, frozen=True)
class RuntimeContext:
    """当前事件处理所需的 Bot 和事件。"""

    bot: BotClient
    event: Event


_current: ContextVar[RuntimeContext | None] = ContextVar("fxbot_runtime_context", default=None)


def bind_event(bot: BotClient, event: Event) -> Token[RuntimeContext | None]:
    """绑定当前异步任务的事件上下文。"""
    if (bot.platform, bot.bot_id) != (event.platform, event.bot_id):
        raise ValueError("客户端与事件的 Bot 身份不一致")
    return _current.set(RuntimeContext(bot=bot, event=event))


def reset_event(token: Token[RuntimeContext | None]) -> None:
    """恢复绑定前的事件上下文。"""
    _current.reset(token)


def current_runtime() -> RuntimeContext:
    """读取当前事件上下文，缺失时明确报错。"""
    value = _current.get()
    if value is None:
        raise RuntimeError("当前不在 FxBot 事件处理上下文中")
    return value


def current_event() -> Event:
    """读取当前事件。"""
    return current_runtime().event
