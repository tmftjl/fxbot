"""暴露给工具的运行时能力。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from dataclasses import dataclass

from ...core import Event, BotClient, ReplySender

if TYPE_CHECKING:
    from ...core.application import Application
    from ...permission.authorization import Authorizer


@dataclass
class ToolRuntime:
    """工具只获得本次调用明确授予的客户端、事件和发送器。"""

    bot: BotClient | None = None
    event: Event | None = None
    sender: ReplySender | None = None
    authorizer: Authorizer | None = None
    application: Application | None = None
    allowed_tools: frozenset[str] | None = None

    def require_bot(self) -> BotClient:
        """获取 bot，不存在时抛错。"""
        if self.bot is None:
            raise RuntimeError("当前工具需要 bot 对象")
        return self.bot

    def require_event(self) -> Event:
        """获取 event，不存在时抛错。"""
        if self.event is None:
            raise RuntimeError("当前工具需要 event 对象")
        return self.event
