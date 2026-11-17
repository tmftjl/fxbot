"""消息与引用中的图片来源提取，不依赖平台实现。"""

from __future__ import annotations

import base64
import binascii

import httpx

from ...core import Event, BotClient, Capability, UnsupportedCapability
from ...utils.http import get_shared_async_client

_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


async def image_sources(bot: BotClient, event: Event) -> list[str | bytes]:
    """返回当前消息与引用消息中的图片，按出现顺序去重。"""
    media = bot.capabilities.require(Capability.MEDIA)
    segments = [segment for segment in event.message.segments if segment.type == "image"]
    if event.reply is not None:
        try:
            quoted = await bot.capabilities.require(Capability.REPLY).get_reply(event)
        except UnsupportedCapability:
            quoted = None
        if quoted is not None:
            segments.extend(segment for segment in quoted.message.segments if segment.type == "image")
    sources: list[str | bytes] = []
    for segment in segments:
        try:
            source = await media.resolve_media(segment)
        except UnsupportedCapability:
            continue
        if isinstance(source, (str, bytes)) and source and source not in sources:
            sources.append(source)
    return sources


async def fetch_image_bytes(source: str | bytes) -> bytes | None:
    """把字节、base64 或 HTTP(S) 来源读取为图片字节。"""
    if isinstance(source, bytes):
        return source
    if not isinstance(source, str) or not source:
        return None
    if source.startswith("base64://"):
        try:
            return base64.b64decode(source[len("base64://") :], validate=True)
        except (binascii.Error, ValueError):
            return None
    if source.startswith("data:"):
        try:
            return base64.b64decode(source.split(",", 1)[1], validate=True)
        except (IndexError, binascii.Error, ValueError):
            return None
    if not source.startswith(("http://", "https://")):
        return None
    try:
        client = await get_shared_async_client()
        response = await client.get(source)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    content = response.content
    if not content or len(content) > _MAX_DOWNLOAD_BYTES:
        return None
    return content
