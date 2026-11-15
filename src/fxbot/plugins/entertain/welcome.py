"""按完整会话键保存的结构化欢迎语。"""

from __future__ import annotations

import io
import re
import uuid
from hashlib import sha256

from PIL import Image

from ...chat.media import load_image_bytes
from ...core.types import Message, SessionTarget, MessageSegment
from ...core.plugin import on_event, on_regex
from ...utils.paths import data_dir
from ...utils.json_file import read_json, write_json
from ...core.capabilities import Capability


class Welcome:
    def __init__(self, application, notifications):
        self.application = application
        self.notifications = notifications
        self.path = data_dir("entertain") / "welcome.json"
        self.images = data_dir("entertain") / "welcome_images"

    def records(self) -> dict:
        records = read_json(self.path, dict)
        if not isinstance(records, dict):
            raise ValueError("欢迎语记录必须为对象")
        for key, record in records.items():
            if (
                SessionTarget.from_key(key).kind != "group"
                or not isinstance(record, dict)
                or set(record) != {"enabled", "segments"}
                or type(record["enabled"]) is not bool
                or not isinstance(record["segments"], list)
            ):
                raise ValueError("欢迎语需要完整群身份及结构化消息")
            for part in record["segments"]:
                if (
                    not isinstance(part, dict)
                    or set(part) != {"type", "value"}
                    or part["type"] not in {"text", "image"}
                    or not isinstance(part["value"], str)
                ):
                    raise ValueError("欢迎语消息段格式错误")
                if part["type"] == "image" and not re.fullmatch(r"[0-9a-f]{32}\.png", part["value"]):
                    raise ValueError("欢迎图片必须是本服务生成的文件名")
        return records

    def directory(self, target):
        return self.images / sha256(target.key().encode("utf-8")).hexdigest()

    def render(self, target, record) -> Message:
        directory = self.directory(target)
        return Message(
            [
                MessageSegment.text(part["value"])
                if part["type"] == "text"
                else MessageSegment.image((directory / part["value"]).read_bytes())
                for part in record["segments"]
            ]
        )

    async def set(self, context) -> None:
        target = context.event.target
        source = context.event.original_message or context.event.message
        parts = []
        staged = []
        prefix_removed = False
        try:
            for segment in source.segments:
                if segment.type == "text":
                    text = segment.text_value()
                    if not prefix_removed:
                        text, count = re.subn(r"^\s*[#＃]设置欢迎\s*", "", text, count=1)
                        prefix_removed = bool(count)
                    if text:
                        parts.append({"type": "text", "value": text})
                elif segment.type == "image":
                    if len(staged) >= 5:
                        raise ValueError("欢迎语最多包含 5 张图片")
                    raw = await load_image_bytes(
                        await context.bot.capabilities.require(Capability.MEDIA).resolve_media(segment),
                        limit=2 * 1024 * 1024,
                    )
                    with Image.open(io.BytesIO(raw)) as image:
                        if image.width * image.height > 16_000_000:
                            raise ValueError("欢迎图片尺寸过大")
                        buffer = io.BytesIO()
                        image.convert("RGBA").save(buffer, format="PNG")
                    name = uuid.uuid4().hex + ".png"
                    directory = self.directory(target)
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / name
                    staged.append(path)
                    path.write_bytes(buffer.getvalue())
                    parts.append({"type": "image", "value": name})
            if not parts:
                await context.finish("请提供欢迎文字或图片")
            records = self.records()
            records[target.key()] = {"enabled": True, "segments": parts}
            write_json(self.path, records)
        except BaseException:
            for path in staged:
                path.unlink(missing_ok=True)
            raise
        await context.send("欢迎语已设置并开启")

    async def show(self, context) -> None:
        record = self.records().get(context.event.target.key())
        await context.send(self.render(context.event.target, record) if record else "尚未设置欢迎语")

    async def toggle(self, context) -> None:
        records = self.records()
        record = records.get(context.event.target.key())
        if record is None:
            await context.finish("请先设置欢迎语")
        record["enabled"] = "开启" in context.arguments
        write_json(self.path, records)
        await context.send("已开启欢迎" if record["enabled"] else "已关闭欢迎")

    async def joined(self, context) -> None:
        event = context.event
        if event.user_id == event.bot_id:
            return
        record = self.records().get(event.target.key())
        if not record or not record["enabled"]:
            return
        message = self.render(event.target, record)
        message.segments.insert(0, MessageSegment.mention(event.user_id))
        await self.notifications.send(
            "entertain",
            event.target,
            message,
            valid=lambda: (
                self.application.is_registered(context.bot) and self.records().get(event.target.key()) == record
            ),
        )


set_welcome_matcher = on_regex(
    r"^[#＃]设置欢迎(?:\s*(.+))?", name="set", display_name="设置欢迎语", level="admin", scene="group"
)


@set_welcome_matcher.handle()
async def set_welcome(context):
    await welcome(context.app_services).set(context)


show_welcome_matcher = on_regex(r"^[#＃]?查看欢迎", name="show", display_name="查看欢迎语", level="all", scene="group")


@show_welcome_matcher.handle()
async def show_welcome(context):
    await welcome(context.app_services).show(context)


enable_welcome_matcher = on_regex(
    r"^[#＃]?开启欢迎", name="enable", display_name="开启欢迎", level="admin", scene="group"
)


@enable_welcome_matcher.handle()
async def enable_welcome(context):
    await welcome(context.app_services).toggle(context)


disable_welcome_matcher = on_regex(
    r"^[#＃]?关闭欢迎", name="disable", display_name="关闭欢迎", level="admin", scene="group"
)


@disable_welcome_matcher.handle()
async def disable_welcome(context):
    await welcome(context.app_services).toggle(context)


joined_matcher = on_event("member_joined", name="welcome", display_name="欢迎新成员", scene="group", priority=12)


@joined_matcher.handle()
async def joined(context):
    await welcome(context.app_services).joined(context)


def welcome(runtime):
    service = Welcome(runtime.application, runtime.notifications)
    service.records()
    return service
