"""按群号保存的结构化欢迎语。"""

from __future__ import annotations

import io
import re
import uuid
from pathlib import Path

from PIL import Image
from loguru import logger

from ...chat.media import load_image_bytes
from ...core.types import Message, MessageSegment
from ...core.plugin import on_event, on_regex
from ...utils.paths import data_dir
from ...utils.json_file import read_json, write_json
from ...core.capabilities import Capability

_PLACEHOLDER = re.compile(r"\[\[WELCOME_IMG:([^\]]+)\]\]")
_PREFIX = re.compile(r"^\s*[#＃]设置欢迎\s*")


def _group_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or ":" in value:
        return ""
    if value in {".", ".."} or any(char in value for char in "/\\"):
        return ""
    return value


def _image_name(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


class Welcome:
    def __init__(self, application, notifications):
        self.application = application
        self.notifications = notifications
        self.path = data_dir("entertain") / "welcome.json"
        self.images = data_dir("welcome")

    def records(self) -> dict[str, dict]:
        data = read_json(self.path, dict)
        if not isinstance(data, dict):
            return {}
        records: dict[str, dict] = {}
        for key, record in data.items():
            group_id = _group_id(key)
            if (
                not group_id
                or not isinstance(record, dict)
                or set(record) != {"enabled", "content"}
                or type(record["enabled"]) is not bool
                or not isinstance(record["content"], str)
            ):
                logger.warning("[welcome] 跳过无效欢迎语: {}", key)
                continue
            if any(not _image_name(match.group(1)) for match in _PLACEHOLDER.finditer(record["content"])):
                logger.warning("[welcome] 跳过无效欢迎图片地址: {}", key)
                continue
            records[group_id] = record
        return records

    def directory(self, target) -> Path:
        group_id = _group_id(target.target_id)
        if not group_id:
            raise ValueError("欢迎语只能配置群会话")
        return self.images / group_id

    def render(self, target, record: dict) -> Message:
        directory = self.directory(target)
        content = record["content"]
        segments = []
        position = 0
        for match in _PLACEHOLDER.finditer(content):
            if match.start() > position:
                segments.append(MessageSegment.text(content[position : match.start()]))
            name = match.group(1)
            try:
                segments.append(MessageSegment.image((directory / name).read_bytes()))
            except OSError:
                segments.append(MessageSegment.text(match.group(0)))
            position = match.end()
        if position < len(content):
            segments.append(MessageSegment.text(content[position:]))
        return Message(segments)

    async def set(self, context) -> None:
        target = context.event.target
        directory = self.directory(target)
        source = context.event.original_message or context.event.message
        parts: list[str] = []
        staged: list[Path] = []
        prefix_removed = False
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for segment in source.segments:
                if segment.type == "text":
                    text = segment.text_value()
                    if not prefix_removed:
                        text, count = _PREFIX.subn("", text, count=1)
                        prefix_removed = bool(count)
                    if text:
                        parts.append(text)
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
                    path = directory / name
                    staged.append(path)
                    path.write_bytes(buffer.getvalue())
                    parts.append(f"[[WELCOME_IMG:{name}]]")
            if not parts:
                await context.finish("请提供欢迎文字或图片")
            content = "".join(parts)
            records = self.records()
            records[target.target_id] = {"enabled": True, "content": content}
            write_json(self.path, records)
        except BaseException:
            for path in staged:
                path.unlink(missing_ok=True)
            raise
        keep = {match.group(1) for match in _PLACEHOLDER.finditer(content)}
        for path in directory.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink(missing_ok=True)
        await context.send("欢迎语已设置并开启")

    async def show(self, context) -> None:
        group_id = context.event.target.target_id
        record = self.records().get(group_id)
        await context.send(self.render(context.event.target, record) if record else "尚未设置欢迎语")

    async def toggle(self, context) -> None:
        group_id = context.event.target.target_id
        records = self.records()
        record = records.get(group_id)
        if record is None:
            await context.finish("请先设置欢迎语")
        record["enabled"] = "开启" in context.arguments
        write_json(self.path, records)
        await context.send("已开启欢迎" if record["enabled"] else "已关闭欢迎")

    async def joined(self, context) -> None:
        event = context.event
        if event.user_id == event.bot_id:
            return
        group_id = event.target.target_id
        record = self.records().get(group_id)
        if not record or not record["enabled"]:
            return
        message = self.render(event.target, record)
        message.segments.insert(0, MessageSegment.mention(event.user_id))
        await self.notifications.send(
            "entertain",
            event.target,
            message,
            valid=lambda: self.application.is_registered(context.bot) and self.records().get(group_id) == record,
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
