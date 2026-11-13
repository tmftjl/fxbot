"""视频解析的原生路由和完整请求生命周期。"""

from __future__ import annotations

import json
import asyncio

import httpx
from loguru import logger

from . import state, config
from ...core import Event, Message, Capability, HandlerContext, MessageSegment
from .sender import send_image_result, send_video_result
from .parsers import ParseError, find_url, parse_url, can_parse_url
from .downloader import DownloadError, download_video, download_images, create_download_dir, cleanup_download_dir
from ...core.plugin import on_regex, on_message
from ...runtime.actions import ActionFailed
from ...core.application import Application
from ...permission.authorization import Authorizer


def find_event_url(event: Event) -> str | None:
    for segment in (event.original_message or event.message).segments:
        if segment.type != "onebot11:json":
            continue
        raw = segment.data.get("data")
        if not isinstance(raw, str):
            continue
        try:
            card = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(card, dict) or not isinstance(card.get("meta"), dict):
            continue
        for key, field in (("detail_1", "qqdocurl"), ("news", "jumpUrl"), ("music", "jumpUrl")):
            value = card["meta"].get(key)
            if isinstance(value, dict) and isinstance(value.get(field), str) and can_parse_url(value[field]):
                return value[field]
    return find_url(event.message.plain_text())


class VideoParser:
    def __init__(self, application: Application, authorizer: Authorizer) -> None:
        self.application = application
        self.authorizer = authorizer
        self.login_lock = asyncio.Lock()
        self.downloads = set()

    def match(self, event: Event) -> str | None:
        if not event.is_message or not config.is_global_enabled():
            return None
        url = find_event_url(event)
        if not url or not can_parse_url(url) or not state.is_group_enabled(event.target):
            return None
        return url

    async def parse(self, context: HandlerContext) -> None:
        if Capability.FORWARD_SEND not in context.bot.capabilities:
            await context.finish("当前平台不支持发送解析结果所需的合并消息")
        processing = await context.send("正在解析媒体，请稍候...")
        directory = create_download_dir()
        self.downloads.add(directory)
        try:
            result = await parse_url(context.arguments)
            if not self.application.is_registered(context.bot) or not await self.authorizer.check(
                "video_parser:parse", context.event
            ):
                return
            if result.video_url:
                if Capability.VIDEO not in context.bot.capabilities:
                    await context.finish("当前平台不支持视频发送")
                path = await download_video(result, directory=directory)
                if not self.application.is_registered(context.bot) or not await self.authorizer.check(
                    "video_parser:parse", context.event
                ):
                    return
                await send_video_result(context, result, path)
            elif result.image_urls:
                paths = await download_images(result, directory=directory)
                if not self.application.is_registered(context.bot) or not await self.authorizer.check(
                    "video_parser:parse", context.event
                ):
                    return
                await send_image_result(context, result, paths)
            else:
                raise ParseError("解析结果没有可发送媒体")
            message_id = processing.get("message_id")
            if message_id is not None:
                try:
                    await context.bot.delete_message(str(message_id))
                except (ActionFailed, TimeoutError, OSError):
                    logger.opt(exception=True).debug("解析已交付，处理中提示撤回失败")
        except (ParseError, DownloadError) as exc:
            await context.send(f"解析失败：{exc}")
        except httpx.HTTPError:
            logger.opt(exception=True).warning("视频平台请求失败")
            await context.send("解析失败：平台请求未完成")
        finally:
            cleanup_download_dir(directory)
            self.downloads.discard(directory)

    async def group_toggle(self, context: HandlerContext) -> None:
        enabled = context.arguments.lstrip("#＃").startswith("开启")
        state.set_group_enabled(context.event.target, enabled)
        await context.finish(f"本群视频解析已{'开启' if enabled else '关闭'}")

    async def global_toggle(self, context: HandlerContext) -> None:
        enabled = "开启" in context.arguments
        config.set_global_enabled(enabled)
        await context.finish(f"全局视频解析已{'开启' if enabled else '关闭'}")

    async def login(self, context: HandlerContext) -> None:
        if self.login_lock.locked():
            await context.finish("B站登录正在进行，请稍后再试")
        from .parsers import bilibili

        async with self.login_lock:
            try:
                image = await bilibili.create_qrcode()
                await context.send(
                    Message([MessageSegment.text("请使用哔哩哔哩客户端扫码登录\n"), MessageSegment.image(image)])
                )
                await context.finish(await bilibili.poll_qrcode(context.send))
            except ParseError as exc:
                await context.finish(str(exc))
            finally:
                bilibili.reset_qrcode()


def parser(runtime):
    config.validate_config(config.REG.storage.read(strict=True))
    state.load_state()
    return VideoParser(runtime.application, runtime.authorizer)


async def has_url(context):
    url = context.service(parser).match(context.event)
    return url is not None


@on_message(name="parse", display_name="链接解析", priority=8, when=has_url)
async def parse(context):
    from dataclasses import replace

    url = context.service(parser).match(context.event)
    if url is not None:
        await context.service(parser).parse(replace(context, arguments=url))


@on_regex(r"^[#＃]?(开启|关闭)解析$", name="group_toggle", display_name="群解析开关", level="admin", scene="group")
async def group_toggle(context):
    await context.service(parser).group_toggle(context)


@on_regex(r"^[#＃]?全局(开启|关闭)解析$", name="global_toggle", display_name="全局解析开关", level="superuser")
async def global_toggle(context):
    await context.service(parser).global_toggle(context)


@on_regex(r"^[#＃]?(?:B站|b站|哔哩哔哩)登录$", name="bilibili_login", display_name="B站登录", level="superuser")
async def bilibili_login(context):
    await context.service(parser).login(context)
