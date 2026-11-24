"""下行帧分发：日志帧只记录，其他帧交给注册表中的适配器。"""

from __future__ import annotations

from loguru import logger

from fxbot.core import ApplicationServices

from .models import MessageSend
from .adapters import find_send
from .adapters.base import SendResult

LOG_LEVELS = {"debug", "info", "warning", "error", "critical", "success", "trace"}


def is_log_frame(msg: MessageSend) -> bool:
    """gsuid_core 的 Bot 日志帧不是可发送消息。"""
    content = msg.content or []
    return bool(content) and isinstance(content[0].type, str) and content[0].type.startswith("log")


def _log_segment(kind: str, data: object) -> None:
    level = kind.split("_")[-1].lower()
    if level in LOG_LEVELS:
        logger.log(level.upper(), "{}", data)
    else:
        logger.info("[gscore][core] {}: {}", kind, data)


def log_frame(msg: MessageSend) -> None:
    """不把日志帧发送到任何平台，只写入本地日志。"""
    content = msg.content or []
    if not content:
        logger.info("[gscore][core] 日志帧来自 {}", msg.bot_id)
        return
    for segment in content:
        kind = segment.type or ""
        if kind.startswith("log"):
            _log_segment(kind, segment.data)
        else:
            logger.info("[gscore][core] {}: {}", kind, segment.data)


async def dispatch_outgoing(
    services: ApplicationServices, msg: MessageSend, *, mapping: object | None = None
) -> SendResult | None:
    """按 bot_id 查找适配器；身份映射生效时先把官方入站的回复路由回官方 Bot。"""
    if is_log_frame(msg):
        log_frame(msg)
        return None
    if mapping is not None:
        msg = mapping.rewrite_outgoing(msg)
    adapter = find_send(msg.bot_id)
    if adapter is None:
        return None
    return await adapter.send(services, msg)
