"""QQ 群合并转发内容的长图渲染。"""

from __future__ import annotations

import io
import asyncio
from typing import Any
from urllib.parse import urljoin

from PIL import Image, ImageDraw
from loguru import logger

from fxbot.utils.http import read_limited, ensure_public_http_url, get_shared_async_client
from fxbot.utils.fonts import draw_text_with_fallback, load_fallback_font_pair, textlength_with_fallback

from .base import media_value, outbound_media
from ..models import Message as WireMessage

PAGE_WIDTH = 900
PAGE_HEIGHT = 4800
PAGE_MARGIN = 24
MAX_PAGES = 5
MAX_IMAGE_BYTES = 15 * 1024 * 1024
TEXT_SIZE = 24
LINE_HEIGHT = 34
BLOCK_SPACING = 16
_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(4)
_REDIRECTS = 3


def _text(data: Any) -> str:
    return data if isinstance(data, str) else "" if data is None else str(data)


async def _download(url: str) -> bytes | None:
    client = await get_shared_async_client()
    current = url
    async with _DOWNLOAD_SEMAPHORE:
        for _ in range(_REDIRECTS + 1):
            await ensure_public_http_url(current)
            async with client.stream("GET", current, follow_redirects=False, timeout=20) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(str(response.url), location)
                    continue
                response.raise_for_status()
                return await read_limited(
                    response.aiter_bytes(), response.headers.get("content-length"), MAX_IMAGE_BYTES
                )
    return None


async def _image_bytes(source: Any) -> bytes | None:
    value = outbound_media(source)
    if isinstance(value, bytes):
        return value
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        try:
            return await _download(value)
        except Exception:
            logger.opt(exception=True).warning("[gscore] QQ 长图素材下载失败: {}", value)
    return None


def _wrap(text: str, width: int, primary, fallback) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for char in paragraph:
            candidate = line + char
            if not line or textlength_with_fallback(candidate, primary, fallback) <= width:
                line = candidate
            else:
                lines.append(line)
                line = char
        lines.append(line)
    return lines


async def _items(frames: list[list[WireMessage]]) -> list[tuple[str, Any]] | None:
    """把帧展开为有序文本/图片；出现其他媒体时返回 None。"""
    items: list[tuple[str, Any]] = []
    sources: list[Any] = []
    for frame in frames:
        for segment in frame:
            if segment.type == "text":
                text = _text(segment.data)
                if text:
                    items.append(("text", text))
            elif segment.type == "image":
                source = media_value(segment.data)
                if source is None:
                    items.append(("text", "[图片无法读取]"))
                else:
                    items.append(("image", None))
                    sources.append(source)
            elif segment.type in {"at", "reply"}:
                continue
            else:
                return None
    loaded = await asyncio.gather(*(_image_bytes(source) for source in sources), return_exceptions=False)
    image_iter = iter(loaded)
    for index, (kind, _value) in enumerate(items):
        if kind == "image":
            image = next(image_iter)
            items[index] = ("image", image) if image is not None else ("text", "[图片无法读取]")
    return items


def _layout(items: list[tuple[str, Any]], primary, fallback) -> list[tuple[str, Any, int]]:
    text_width = PAGE_WIDTH - PAGE_MARGIN * 2
    text_height = 0
    image_count = 0
    for kind, value in items:
        if kind == "text":
            text_height += max(1, len(_wrap(value, text_width, primary, fallback))) * LINE_HEIGHT
            text_height += BLOCK_SPACING
        else:
            image_count += 1
    available = MAX_PAGES * PAGE_HEIGHT - text_height - PAGE_MARGIN * 2 * MAX_PAGES
    image_height = 300
    if image_count:
        image_height = max(100, min(360, available // image_count - BLOCK_SPACING))
    blocks: list[tuple[str, Any, int]] = []
    for kind, value in items:
        if kind == "text":
            for line in _wrap(value, text_width, primary, fallback):
                blocks.append(("text", line, LINE_HEIGHT))
            blocks.append(("space", None, BLOCK_SPACING))
            continue
        try:
            image = Image.open(io.BytesIO(value)).convert("RGB")
        except Exception:
            for line in _wrap("[图片无法读取]", text_width, primary, fallback):
                blocks.append(("text", line, LINE_HEIGHT))
            blocks.append(("space", None, BLOCK_SPACING))
            continue
        image.thumbnail((text_width, image_height))
        blocks.append(("image", image, image.height))
        blocks.append(("space", None, BLOCK_SPACING))
    return blocks


def _render(blocks: list[tuple[str, Any, int]], primary, fallback) -> list[bytes]:
    pages: list[Image.Image] = []
    bottoms: list[int] = []
    page = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), "white")
    draw = ImageDraw.Draw(page)
    y = PAGE_MARGIN
    for kind, value, height in blocks:
        if y + height > PAGE_HEIGHT - PAGE_MARGIN and y > PAGE_MARGIN:
            bottoms.append(y)
            pages.append(page)
            page = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), "white")
            draw = ImageDraw.Draw(page)
            y = PAGE_MARGIN
        if kind == "text":
            draw_text_with_fallback(draw, (PAGE_MARGIN, y), value, primary, fallback, fill=(32, 32, 32))
        elif kind == "image":
            page.paste(value, (PAGE_MARGIN, y))
        y += height
    bottoms.append(y)
    pages.append(page)
    if len(pages) > MAX_PAGES:
        pages = pages[:MAX_PAGES]
        bottoms = bottoms[:MAX_PAGES]
        last = pages[-1]
        draw = ImageDraw.Draw(last)
        draw_text_with_fallback(
            draw,
            (PAGE_MARGIN, PAGE_MARGIN),
            "内容过长，后续内容已截断",
            primary,
            fallback,
            fill=(180, 40, 40),
        )
    result: list[bytes] = []
    for image, bottom in zip(pages, bottoms):
        cropped = image.crop((0, 0, PAGE_WIDTH, min(PAGE_HEIGHT, max(1, bottom + PAGE_MARGIN))))
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG")
        result.append(buffer.getvalue())
    return result


async def render_pages(frames: list[list[WireMessage]]) -> list[bytes] | None:
    """把文本与图片按顺序渲染为最多 5 张长图；不支持的内容返回 None。"""
    items = await _items(frames)
    if not items:
        return None
    primary, fallback = load_fallback_font_pair(size=TEXT_SIZE)
    blocks = _layout(items, primary, fallback)
    if not blocks:
        return None
    return _render(blocks, primary, fallback)
