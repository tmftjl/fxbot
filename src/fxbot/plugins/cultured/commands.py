"""图库命令匹配与图片发送。"""

from __future__ import annotations

import re
import asyncio
from typing import Any
from collections.abc import Callable

from loguru import logger

from ...core import Capability, Message, HandlerContext, MessageSegment
from .config import load_cfg, face_list, load_all_commands, random_local_image
from ...utils.http import read_limited, ensure_public_http_url, get_shared_async_client
from ...core.plugin import on_regex

MAX_GALLERY_BYTES = 20 * 1024 * 1024


def _command_names() -> list[str]:
    """读取所有 API 图库命令名。"""
    names: list[str] = []
    for item in load_all_commands():
        name = str(item["name"]).strip()
        if name:
            names.append(name)
    return names


def _build_api_regex() -> re.Pattern[str] | None:
    """构造 API 图库命令匹配表达式。"""
    names = _command_names()
    if not names:
        return None
    joined = "|".join(re.escape(name) for name in names)
    return re.compile(rf"^(?:#|＃|/)?(?:来张|看看|随机)\s*({joined})$", re.I)


def _build_picture(url: str, response_text: str | None = None) -> Message:
    """构造图片回复消息。"""
    parts: list[MessageSegment] = []
    if response_text:
        parts.append(MessageSegment.text(response_text))
    parts.append(MessageSegment.image(url))
    return Message(parts)


async def _download_image(url: str) -> bytes:
    """平台不代下 URL 时由本机下载图库图片。"""
    await ensure_public_http_url(url)
    client = await get_shared_async_client()
    async with client.stream("GET", url, follow_redirects=True, timeout=30) as response:
        response.raise_for_status()
        return await read_limited(response.aiter_bytes(), response.headers.get("content-length"), MAX_GALLERY_BYTES)


async def _localize_images(context: HandlerContext, message: Message) -> Message:
    """平台不接受远程 URL 时先下载成字节，避免腾讯代下被拒（40093007）。"""
    if Capability.MEDIA_URL in context.bot.capabilities:
        return message
    segments: list[MessageSegment] = []
    for segment in message.segments:
        source = segment.data.get("file") if segment.type == "image" else None
        if isinstance(source, str) and source.startswith(("http://", "https://")):
            segments.append(MessageSegment.image(await _download_image(source)))
        else:
            segments.append(segment)
    return Message(segments)


def _create_api_handler(command: dict[str, Any]) -> Callable[[], Any]:
    """根据配置创建 API 图库处理器。"""
    url = str(command["url"]).strip()
    method = str(command["method"]).strip().lower()
    response_text = command["response_text"]
    regex_pattern = str(command["regex"])

    async def _fetch_text() -> str:
        client = await get_shared_async_client()
        response = await client.get(url)
        response.raise_for_status()
        return response.text.replace("\\", "/").strip()

    if method == "direct":
        return lambda: _build_picture(url, response_text)

    if method == "get_text":

        async def _handle_get_text() -> Message:
            return _build_picture(await _fetch_text(), response_text)

        return _handle_get_text

    if method == "get_text_regex":

        async def _handle_get_text_regex() -> Message | None:
            match = re.search(regex_pattern, await _fetch_text())
            if not match:
                return None
            return _build_picture(match.group(0), response_text)

        return _handle_get_text_regex

    logger.warning(f"[Cultured] 未知图库获取模式: {method}")
    return lambda: None


def _api_handlers() -> list[tuple[str, Callable[[], Any]]]:
    """加载 API 图库处理器。"""
    handlers: list[tuple[str, Callable[[], Any]]] = []
    for command in load_all_commands():
        name = str(command["name"]).strip()
        if name:
            handlers.append((name, _create_api_handler(command)))
    return handlers


async def api_picture_rule(context: HandlerContext) -> bool:
    if not load_cfg()["random_picture_open"]:
        return False
    name = context.match.group(1).casefold()
    return any(item.casefold() == name for item in _command_names())


async def local_picture_rule(context: HandlerContext) -> bool:
    if not load_cfg()["random_picture_open"]:
        return False
    name = context.match.group(1)
    # 同名时仅由 API 图库处理，避免同优先级响应器重复发送。
    return name in face_list() and not any(item.casefold() == name.casefold() for item in _command_names())


api_cmd = on_regex(
    r"^(?:#|＃|/)?(?:来张|看看|随机)\s*(\S+)\s*$",
    rule=api_picture_rule,
    name="pictures_api",
    display_name="看看腿",
    priority=5,
    block=True,
    level="member",
    scene="all",
)

list_cmd = on_regex(
    r"^[#＃]?(?:cultured|表情包)列表",
    name="pictures_list",
    display_name="表情包列表",
    priority=5,
    block=True,
    level="member",
    scene="all",
)

picture_cmd = on_regex(
    r"^(?:#|＃|/)?(?:来张|看看|随机)\s*(\S+)\s*$",
    rule=local_picture_rule,
    name="pictures_local",
    display_name="随机本地表情",
    priority=5,
    block=True,
    level="member",
    scene="all",
)


@list_cmd.handle()
async def _handle_list(context: HandlerContext) -> None:
    """发送图库列表。"""
    faces = face_list()
    await context.finish("表情列表：\n" + ("、".join(faces) or "(空)") + "\n\n使用 #随机<名称>")


@api_cmd.handle()
async def _handle_api_picture(context: HandlerContext) -> None:
    """发送 API 图库图片。"""
    if not bool(load_cfg()["random_picture_open"]):
        await context.finish()

    text = context.event.message.plain_text().strip()
    api_regex = _build_api_regex()
    if api_regex is not None:
        match = api_regex.match(text)
        if match:
            target = match.group(1)
            for name, handler in _api_handlers():
                if name.lower() == target.lower():
                    try:
                        result = handler()
                        if asyncio.iscoroutine(result):
                            result = await result
                    except Exception:
                        logger.opt(exception=True).warning(f"[Cultured] API 图库 {name} 请求失败")
                        await context.finish("图库接口请求失败")
                    if result:
                        try:
                            result = await _localize_images(context, result)
                        except Exception:
                            logger.opt(exception=True).warning(f"[Cultured] 图库图片下载失败: {name}")
                            await context.finish("图库图片下载失败")
                        await context.finish(result)
                    await context.finish()
    await context.finish()


@picture_cmd.handle()
async def _handle_picture(context: HandlerContext) -> None:
    """发送随机本地图库图片。"""
    if not bool(load_cfg()["random_picture_open"]):
        return

    text = context.event.message.plain_text().strip()
    match = re.match(r"^(?:#|＃|/)?(?:来张|看看|随机)\s*(\S+)", text)
    name = match.group(1) if match else ""
    if name in face_list():
        path = random_local_image(name)
        if path is not None:
            await context.finish(Message([MessageSegment.image(path.read_bytes())]))
