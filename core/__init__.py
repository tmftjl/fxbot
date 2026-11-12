"""FxBot 独立运行时的无框架核心契约。"""

from __future__ import annotations

from .types import (
    Event,
    Message,
    BotClient,
    ReplyTarget,
    SessionTarget,
    MessageSegment,
    UnsupportedCapability,
)
from .router import Router, DispatchResult, HandlerContext
from .context import bind_event, reset_event, current_event, current_runtime

__all__ = [
    "BotClient",
    "Event",
    "Message",
    "MessageSegment",
    "ReplyTarget",
    "SessionTarget",
    "UnsupportedCapability",
    "Router",
    "DispatchResult",
    "HandlerContext",
    "bind_event",
    "current_event",
    "current_runtime",
    "reset_event",
]
