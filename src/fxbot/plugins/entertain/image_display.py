"""应用级图片外显策略，不修改平台消息类。"""

from __future__ import annotations

import random

import httpx

from .config import get_config, save_config, cfg_image_display
from ...core.types import Message, MessageSegment
from ...utils.http import get_shared_async_client
from ...core.plugin import on_regex, outgoing, scheduled


class ImageDisplay:
    def __init__(self):
        self.cached_text = "Ciallo~"

    def transform(self, bot, target, message: Message) -> Message:
        cfg = cfg_image_display()
        if bot.platform != "onebot11" or not cfg["enabled"]:
            return message
        text = cfg["text"] if cfg["type"] == 1 else self.cached_text if cfg["type"] == 2 else random.choice(cfg["list"])
        return Message(
            [
                MessageSegment("image", {**part.data, "summary": text}) if part.type == "image" else part
                for part in message.segments
            ]
        )

    async def refresh(self) -> None:
        cfg = cfg_image_display()
        if not cfg["enabled"] or cfg["type"] != 2 or not cfg["api"]:
            return
        try:
            response = await (await get_shared_async_client()).get(cfg["api"])
            response.raise_for_status()
        except httpx.HTTPError:
            return
        if text := response.text.strip():
            self.cached_text = text[:512]

    async def handle(self, context) -> None:
        if context.bot.platform != "onebot11":
            await context.finish("当前平台不支持图片外显")
        cfg = get_config()
        cfg["image_display"]["enabled"] = "开启" in context.arguments
        save_config(cfg)
        await context.send("已开启图片外显" if cfg["image_display"]["enabled"] else "已关闭图片外显")


toggle_image_display_matcher = on_regex(
    r"^[#＃](开启|关闭)图片外显", name="toggle_image_display", display_name="图片外显开关", level="superuser"
)


@toggle_image_display_matcher.handle()
async def toggle_image_display(context):
    await display.handle(context)


display = ImageDisplay()


@outgoing
def image_summary(bot, target, message):
    return display.transform(bot, target, message)


@scheduled(interval=600, name="image_display")
async def refresh(context):
    await display.refresh()
