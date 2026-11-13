"""FxBot 独立运行时的无框架核心契约。"""

from __future__ import annotations

from .types import (
    Event,
    Message,
    BotClient,
    Capability,
    ReplySender,
    ReplyTarget,
    QuotedMessage,
    SessionTarget,
    HistoryMessage,
    MessageSegment,
    UnsupportedCapability,
)
from .router import Router, DispatchResult, HandlerContext
from .context import bind_event, reset_event, current_event, current_runtime
from .commands import CommandDeclaration, on_regex, on_command, collect_commands

__all__ = [
    "BotClient",
    "Event",
    "Message",
    "HistoryMessage",
    "MessageSegment",
    "ReplyTarget",
    "SessionTarget",
    "UnsupportedCapability",
    "Capability",
    "QuotedMessage",
    "ReplySender",
    "Router",
    "DispatchResult",
    "HandlerContext",
    "bind_event",
    "current_event",
    "current_runtime",
    "reset_event",
    "on_command",
    "on_regex",
    "CommandDeclaration",
    "collect_commands",
]
