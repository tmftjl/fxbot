"""QQ 专有消息段，保持核心 Message 不感知官方消息字段。"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from ...core import MessageSegment


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
    if type(state) is not int or state not in {1, 10, 11, 20} or type(index) is not int or index < 0:
        raise ValueError("QQ 流式状态或索引无效")
    values = {"state": state, "index": index, "id": id, "reset": reset}
    return MessageSegment("qq:stream", {key: value for key, value in values.items() if value is not None})


def prompt_keyboard(value: dict) -> MessageSegment:
    return MessageSegment("qq:prompt_keyboard", deepcopy(value))


def action_button(value: dict) -> MessageSegment:
    return MessageSegment("qq:action_button", deepcopy(value))


def media(file_info: str) -> MessageSegment:
    if not isinstance(file_info, str) or not file_info:
        raise ValueError("QQ media 需要有效 file_info")
    return MessageSegment("qq:media", {"file_info": file_info})
