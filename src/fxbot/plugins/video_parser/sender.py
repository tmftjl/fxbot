"""解析媒体以原生消息节点发送，文件所有权留在本次下载任务。"""

from __future__ import annotations

from pathlib import Path

from .types import VideoResult
from ...core import Message, HandlerContext, MessageSegment
from .config import cfg_general


def _summary(result: VideoResult) -> str:
    lines = [f"{result.platform} | {result.display_title}"]
    if result.duration:
        lines.append(f"时长：{int(result.duration)} 秒")
    return "\n".join(lines)


async def send_video_result(context: HandlerContext, result: VideoResult, video_path: Path) -> None:
    messages = [Message.text(_summary(result))]
    if result.cover_url and cfg_general()["send_cover"]:
        messages.append(Message([MessageSegment.image(result.cover_url)]))
    source = video_path.read_bytes() if cfg_general()["use_base64"] else video_path.resolve().as_uri()
    messages.append(Message([MessageSegment("video", {"file": source})]))
    await context.bot.send_forward(context.event.target, messages)


async def send_image_result(context: HandlerContext, result: VideoResult, image_paths: list[Path]) -> None:
    messages = [Message.text(_summary(result))]
    messages.extend(Message([MessageSegment.image(path.read_bytes())]) for path in image_paths)
    await context.bot.send_forward(context.event.target, messages)
