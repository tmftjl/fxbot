"""解析媒体优先以合并转发发送；平台不支持时降级为逐条发送。"""

from __future__ import annotations

from pathlib import Path

from .types import VideoResult
from ...core import Message, HandlerContext, MessageSegment
from .config import cfg_general
from ...utils.tz import as_shanghai
from ...core.capabilities import Capability


def _summary(result: VideoResult) -> str:
    lines = [f"{result.platform} | {result.display_title}"]
    if result.author_name:
        lines.append(f"作者：{result.author_name}")
    if result.published_at:
        lines.append(f"发布时间：{as_shanghai(result.published_at):%Y-%m-%d %H:%M}")
    if result.duration:
        lines.append(f"时长：{int(result.duration)} 秒")
    return "\n".join(lines)


async def _deliver(context: HandlerContext, messages: list[Message]) -> None:
    """能合并转发就合并，否则按原顺序逐条发送。"""
    if Capability.FORWARD_SEND in context.bot.capabilities:
        await context.bot.capabilities.require(Capability.FORWARD_SEND).send_forward(context.event.target, messages)
        return
    for message in messages:
        await context.send(message)


def _video_segment(context: HandlerContext, video_path: Path) -> MessageSegment:
    """与原版一致：本地路径交给平台读取；开启 base64 时内联字节。"""
    if cfg_general()["use_base64"]:
        data: dict[str, object] = {"file": video_path.read_bytes()}
        if context.bot.platform == "qq":
            # QQ 官方按文件名识别富媒体类型，字节内联时需要显式带上文件名。
            data["name"] = video_path.name
        return MessageSegment("video", data)
    if context.bot.platform == "qq":
        return MessageSegment("video", {"file": video_path})
    return MessageSegment("video", {"file": video_path.resolve().as_uri()})


async def send_media_result(
    context: HandlerContext,
    result: VideoResult,
    *,
    video_path: Path | None = None,
    image_paths: list[Path] | None = None,
    audio_path: Path | None = None,
) -> None:
    """按摘要、封面、视频、图片、音频的顺序发送解析结果。"""
    messages = [Message.text(_summary(result))]
    if result.cover_url and cfg_general()["send_cover"]:
        messages.append(Message([MessageSegment.image(result.cover_url)]))
    if video_path is not None:
        messages.append(Message([_video_segment(context, video_path)]))
    messages.extend(Message([MessageSegment.image(path.read_bytes())]) for path in image_paths or [])
    if audio_path is not None:
        messages.append(Message([MessageSegment.voice(audio_path.read_bytes())]))
    await _deliver(context, messages)


async def send_video_result(context: HandlerContext, result: VideoResult, video_path: Path) -> None:
    await send_media_result(context, result, video_path=video_path)


async def send_image_result(context: HandlerContext, result: VideoResult, image_paths: list[Path]) -> None:
    await send_media_result(context, result, image_paths=image_paths)
