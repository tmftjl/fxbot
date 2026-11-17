"""入站事件到 gsuid 协议的转换入口，只依赖适配器注册表。"""

from __future__ import annotations

from fxbot.core import Event, BotClient, ApplicationServices

from .models import Message as WireMessage, MessageReceive
from .adapters import find_event
from .adapters.base import user_pm


async def build_receive(
    event: Event,
    *,
    bot: BotClient,
    services: ApplicationServices,
    reply_image: bool,
) -> MessageReceive | None:
    """按平台适配器构造上行事件；未知平台返回 None。"""
    adapter = find_event(event)
    if adapter is None:
        return None
    return await adapter.build_receive(event, bot=bot, services=services, reply_image=reply_image)


def build_meta_receive(event: Event, *, services: ApplicationServices) -> MessageReceive | None:
    """把入群/退群/poke 等通知转换为单段 meta-* 上行事件。"""
    adapter = find_event(event)
    if adapter is None:
        return None
    meta = adapter.build_meta(event)
    if meta is None:
        return None
    scope = adapter.receive_scope(event)
    group_id = meta.data.get("group_id") or scope.group_id
    user_id = meta.data.get("user_id")
    return MessageReceive(
        bot_id=meta.bot_id or adapter.receive_bot_id(event),
        bot_self_id=event.bot_id,
        user_type=meta.user_type or scope.user_type,
        group_id=str(group_id) if group_id is not None else None,
        user_id=str(user_id) if user_id is not None else event.user_id,
        sender={},
        content=[WireMessage(f"meta-{meta.name}", meta.data)],
        user_pm=user_pm(event, services),
    )
