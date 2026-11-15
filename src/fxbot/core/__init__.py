"""FxBot 独立运行时的无框架核心契约。"""

from __future__ import annotations

from .rule import Rule
from .types import (
    Event,
    Message,
    BotClient,
    ReplySender,
    ReplyTarget,
    QuotedMessage,
    SessionTarget,
    HistoryMessage,
    MessageSegment,
    UnsupportedCapability,
)
from .router import Router, DispatchResult, HandlerContext
from .binding import State, CommandArguments
from .context import bind_event, reset_event, current_event, current_event_context
from .matcher import Matcher
from .commands import on_event, on_regex, on_command, on_message
from .matching import MatchResult
from .contracts import ApplicationServices
from .permission import Permission
from .capabilities import Capability, Capabilities

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
    "Capabilities",
    "QuotedMessage",
    "ReplySender",
    "Router",
    "DispatchResult",
    "HandlerContext",
    "bind_event",
    "current_event",
    "current_event_context",
    "reset_event",
    "on_command",
    "on_regex",
    "on_event",
    "on_message",
    "Matcher",
    "MatchResult",
    "CommandArguments",
    "ApplicationServices",
    "State",
    "Rule",
    "Permission",
]
