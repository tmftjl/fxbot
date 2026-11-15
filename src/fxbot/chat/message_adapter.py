"""原生事件到聊天请求的边界，业务不识别协议消息结构。"""

from __future__ import annotations

import asyncio
from uuid import uuid4
from datetime import datetime
from dataclasses import dataclass

import httpx
from loguru import logger

from fxbot.core.errors import ActionFailed

from ..core import Event, BotClient, Capability, QuotedMessage, MessageSegment, UnsupportedCapability
from .media import load_images, max_image_count
from .types import MentionRef, ChatRequest, TriggerKind, InboundSegment
from .voice import transcribe_event
from ..utils.tz import now, to_naive_utc
from ..utils.cache import cache
from .session_hooks import resolve_session_id
from .message_context import quote_preview

_RESOURCE_ERRORS = (UnsupportedCapability, ActionFailed, TimeoutError, OSError, httpx.HTTPError)


def _segments(event: Event) -> list[MessageSegment]:
    return (event.original_message or event.message).segments


async def _image_entries(bot: BotClient, segments: list[MessageSegment]) -> list[tuple[str, str | bytes, bool]]:
    entries = []
    for index, segment in enumerate(segments):
        if segment.type == "image":
            try:
                source = await bot.capabilities.require(Capability.MEDIA).resolve_media(segment)
            except _RESOURCE_ERRORS:
                logger.opt(exception=True).debug("[Chat] 图片来源不可读取")
                continue
            entries.append((str(index), source, segment.data.get("is_emoji") is True))
    return entries


async def emoji_sources_for_event(bot: BotClient, event: Event) -> list[str | bytes]:
    """解析明确标记的表情图片。"""
    return list(dict.fromkeys(source for _, source, emoji in await _image_entries(bot, _segments(event)) if emoji))


async def image_sources_for_event(
    bot: BotClient, event: Event, reply: QuotedMessage | None = None
) -> list[str | bytes]:
    """当前消息没有图片时才读取引用图片。"""
    entries = await _image_entries(bot, _segments(event))
    if not entries and reply is not None:
        entries = await _image_entries(bot, reply.message.segments)
    return list(dict.fromkeys(source for _, source, _ in entries))[: max_image_count()]


async def memory_image_sources(bot: BotClient, event: Event) -> list[tuple[str, str | bytes]]:
    """仅登记当前消息原图，保留组件下标并排除表情。"""
    return [(path, source) for path, source, emoji in await _image_entries(bot, _segments(event)) if not emoji]


def _quote_snapshot(reply: QuotedMessage | None) -> dict[str, str] | None:
    if reply is None:
        return None
    return {
        "message_id": reply.message_id,
        "sender_id": reply.sender_id or "",
        "sender_name": " ".join(reply.sender_name.split())[:32],
        "text": " ".join(reply.message.plain_text().split())[:500],
    }


async def _reply_info(bot: BotClient, event: Event) -> QuotedMessage | None:
    if event.reply is None or Capability.REPLY not in bot.capabilities:
        return None
    key = ("chat_reply_info", event.target.key(), event.reply.message_id)
    saved = cache.get(key)
    if saved is not None:
        return saved["info"]
    try:
        info = await bot.capabilities.require(Capability.REPLY).get_reply(event)
    except _RESOURCE_ERRORS:
        logger.opt(exception=True).warning("[Chat] 引用消息暂不可读取")
        return None
    cache.set(key, {"info": info}, ttl=60)
    return info


async def read_quote_snapshot(bot: BotClient, event: Event) -> dict[str, str] | None:
    """普通历史和 Focus 共用已验证的引用结果。"""
    return _quote_snapshot(await _reply_info(bot, event))


def mention_flags(bot: BotClient, event: Event) -> tuple[bool, bool]:
    """全体提及不算针对个人的提及。"""
    targets = set((event.original_message or event.message).mentions())
    return event.to_me or bot.bot_id in targets, event.is_group and bool(targets - {bot.bot_id})


async def _member_name(bot: BotClient, group_id: str, user_id: str) -> str:
    try:
        return " ".join(
            (await bot.capabilities.require(Capability.MEMBER_LOOKUP).member_name(group_id, user_id)).split()
        )[:32]
    except _RESOURCE_ERRORS:
        logger.opt(exception=True).debug("[Chat] 成员昵称查询暂不可用")
        return ""


async def collect_mentions(
    bot: BotClient, event: Event, group_id: str | None, *, resolve_names: bool = True
) -> list[MentionRef]:
    """只查询明确支持成员查询的平台。"""
    if not group_id:
        return []
    refs: dict[str, MentionRef] = {}
    for segment in _segments(event):
        if segment.type != "mention":
            continue
        target = segment.data["user_id"]
        if target != bot.bot_id and target not in refs:
            refs[target] = MentionRef(target, " ".join(segment.data.get("name", "").split())[:32])
        if len(refs) >= 10:
            break
    missing = [
        ref for ref in refs.values() if not ref.name and resolve_names and Capability.MEMBER_LOOKUP in bot.capabilities
    ]
    names = await asyncio.gather(*(_member_name(bot, group_id, ref.user_id) for ref in missing))
    for ref, name in zip(missing, names):
        ref.name = name
    return list(refs.values())


def message_text(event: Event) -> str:
    """纯转发和纯提及使用准确的内容占位。"""
    text = event.message.plain_text()
    if text:
        return text
    if any(segment.type == "forward" for segment in _segments(event)):
        return "[合并转发消息]"
    return "[@了你]" if event.is_group and event.to_me else ""


def platform_for_bot(bot: BotClient) -> str:
    """平台身份由连接确定。"""
    return bot.platform


def session_id_for_event(bot: BotClient, event: Event) -> str:
    """会话不得跨越连接边界。"""
    if (bot.platform, bot.bot_id) != (event.platform, event.bot_id):
        raise ValueError("事件不属于当前 Bot 连接")
    return resolve_session_id(bot, event, event.target.key())


@dataclass(frozen=True)
class MessageIdentity:
    """平台原始身份，不以内容哈希或随机值代替。"""

    session_id: str
    event_id: str | None
    message_id: str | None
    reply_to_message_id: str | None
    dedupe_key: str
    platform: str = ""


def event_identity(bot: BotClient, event: Event) -> MessageIdentity:
    """消息必须带平台消息 ID 或事件 ID。"""
    if event.message_id:
        dedupe_key = f"msg:{event.message_id}"
    elif event.event_id:
        dedupe_key = f"event:{event.event_id}"
    else:
        raise ValueError("聊天消息缺少平台身份")
    return MessageIdentity(
        session_id_for_event(bot, event),
        event.event_id,
        event.message_id,
        event.reply.message_id if event.reply else None,
        dedupe_key,
        event.platform,
    )


def message_occurred_at(event: Event) -> datetime:
    """平台时间已在协议边界校验。"""
    return to_naive_utc(event.occurred_at)


async def adapt_message_event(
    bot: BotClient, event: Event, *, trigger_kind: TriggerKind, include_quote: bool = True
) -> ChatRequest:
    """媒体只解析一次，原图记忆不混入引用图片。"""
    received_at = to_naive_utc(now())
    identity = event_identity(bot, event)
    group_id = event.target.target_id if event.is_group else None
    reply = await _reply_info(bot, event) if include_quote else None
    mentions, voice_text, entries = await asyncio.gather(
        collect_mentions(bot, event, group_id, resolve_names=include_quote),
        transcribe_event(bot, event),
        _image_entries(bot, _segments(event)),
    )
    memory_images = [(path, source) for path, source, emoji in entries if not emoji]
    image_entries = entries
    if not entries and reply is not None:
        image_entries = await _image_entries(bot, reply.message.segments)
    sources = list(dict.fromkeys(source for _, source, _ in image_entries))[: max_image_count()]
    snapshot = _quote_snapshot(reply)
    return ChatRequest(
        session_id=identity.session_id,
        turn_id=uuid4().hex,
        dedupe_key=identity.dedupe_key,
        trigger_kind=trigger_kind,
        proactive=trigger_kind == "proactive",
        event_id=identity.event_id,
        message_id=identity.message_id,
        reply_to_message_id=identity.reply_to_message_id,
        occurred_at=message_occurred_at(event),
        user_id=event.user_id,
        user_name=event.user_name,
        group_id=group_id,
        text=" ".join(part for part in (message_text(event), voice_text) if part),
        segments=[InboundSegment(segment.type, dict(segment.data)) for segment in _segments(event)],
        images=await load_images(sources),
        quoted_text=quote_preview(snapshot),
        platform=event.platform,
        metadata={
            "event": event,
            "received_at": received_at.isoformat(),
            "sender_role": event.metadata.get("sender_role"),
            "quote_snapshot": snapshot,
            "raw_images": [source for _, source in memory_images],
            "raw_image_paths": [path for path, _ in memory_images],
        },
        mentions=mentions,
    )
