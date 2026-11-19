"""QQ 专有消息段，保持核心 Message 不感知官方消息字段。"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from ...core import MessageSegment
from .models import Media, MessageStream


def emoji(id: str) -> MessageSegment:
    return MessageSegment("qq:emoji", {"id": id})


def markdown(value: str | dict) -> MessageSegment:
    return MessageSegment("qq:markdown", {"content": value} if isinstance(value, str) else deepcopy(value))


def keyboard(value: dict) -> MessageSegment:
    return MessageSegment("qq:keyboard", deepcopy(value))


def ark(value: dict) -> MessageSegment:
    return MessageSegment("qq:ark", deepcopy(value))


def embed(value: dict) -> MessageSegment:
    return MessageSegment("qq:embed", deepcopy(value))


def stream(
    *, state: Literal[1, 10, 11, 20], index: int, id: str | None = None, reset: bool | None = None
) -> MessageSegment:
    values = {"state": state, "index": index, "id": id, "reset": reset}
    return MessageSegment("qq:stream", MessageStream.model_validate(values).model_dump(exclude_none=True))


def prompt_keyboard(value: dict) -> MessageSegment:
    return MessageSegment("qq:prompt_keyboard", deepcopy(value))


def action_button(value: dict) -> MessageSegment:
    return MessageSegment("qq:action_button", deepcopy(value))


def media(file_info: str) -> MessageSegment:
    return MessageSegment("qq:media", Media(file_info=file_info).model_dump())


def mention_channel(channel_id: str) -> MessageSegment:
    return MessageSegment("qq:mention_channel", {"channel_id": channel_id})
