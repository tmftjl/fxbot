"""缓存 QQ 会话最近消息，补齐官方事件不回推的引用原文。"""

from __future__ import annotations

from typing import Any
from collections import deque, defaultdict

from ...core import Event, Message, BotClient, MessageSegment

RECENT_LIMIT = 21  # 当前事件会先入队，额外一位确保此前 20 条仍可引用
_recent: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=RECENT_LIMIT))


def _data(event: Event) -> dict[str, Any]:
    value = event.metadata.get("qq_data")
    return value if isinstance(value, dict) else {}


def _ext_value(event: Event, name: str) -> str:
    scene = _data(event).get("message_scene")
    if not isinstance(scene, dict):
        return ""
    for value in scene.get("ext") or []:
        if isinstance(value, str) and value.startswith(f"{name}="):
            return value.removeprefix(f"{name}=")
    return ""


def _key(bot: BotClient, event: Event) -> str:
    target = event.target
    if target.kind in {"group", "channel"}:
        return f"{bot.bot_id}:scene:{target.target_id}"
    if target.kind == "private" and target.target_id.startswith("dms/"):
        channel_id = event.metadata.get("channel_id")
        return f"{bot.bot_id}:scene:{channel_id or target.target_id}"
    return f"{bot.bot_id}:user:{target.target_id}"


def _indices(event: Event) -> list[str]:
    """提取当前消息的全部可引用索引，不混入被引用消息索引。"""
    data = _data(event)
    values = [_ext_value(event, "msg_idx")]
    if data.get("msg_idx") is not None:
        values.append(str(data["msg_idx"]))
    if not _ext_value(event, "ref_msg_idx"):
        elements = data.get("msg_elements")
        if isinstance(elements, list):
            values.extend(
                str(element["msg_idx"])
                for element in elements
                if isinstance(element, dict) and element.get("msg_idx") is not None
            )
    return list(dict.fromkeys(value for value in values if value))


def _reply_index(event: Event) -> str:
    quoted = event.metadata.get("qq_reply")
    if isinstance(quoted, dict) and quoted.get("msg_idx") is not None:
        return str(quoted["msg_idx"])
    return _ext_value(event, "ref_msg_idx")


def _images(message: Message) -> list[str | bytes]:
    result: list[str | bytes] = []
    for segment in message.segments:
        if segment.type != "image":
            continue
        source = segment.data.get("url") or segment.data.get("file")
        if isinstance(source, str) and source and not source.startswith(("base64://", "data:")):
            result.append(source)
        elif isinstance(source, bytes):
            result.append(source)
    return list(dict.fromkeys(result))


def _texts(message: Message) -> list[str]:
    text = "".join(segment.text_value() for segment in message.segments if segment.type == "text").strip()
    return [text] if text else []


def _matches(stored: dict[str, Any], item: dict[str, Any], by_id: bool) -> bool:
    if by_id:
        return stored.get("id") == item["id"]
    return bool(set(stored.get("msg_indices", ())) & set(item["msg_indices"]))


def _store(key: str, item: dict[str, Any], *, by_id: bool = False) -> None:
    if not key:
        return
    existing = next((stored for stored in _recent[key] if _matches(stored, item, by_id)), None)
    if existing is not None:
        existing.update(item)
    else:
        _recent[key].append(item)


def remember_event(bot: BotClient, event: Event) -> None:
    """记录收到消息的 ID、引用索引、作者、文本和图片。"""
    if not event.message_id:
        return
    indices = _indices(event)
    if not indices:
        return
    message = event.original_message or event.message
    _store(
        _key(bot, event),
        {
            "id": event.message_id,
            "msg_indices": indices,
            "sender_id": event.user_id,
            "sender_name": event.user_name,
            "images": _images(message),
            "texts": _texts(message),
        },
        by_id=True,
    )


def remember_sent(
    bot: BotClient,
    group_openid: str,
    message: Message,
    message_id: str | None,
    ref_idx: str | None,
) -> None:
    """记录 Bot 自己发出的群消息：QQ 不回推自己的消息，只能靠发送响应的引用索引。"""
    if not bot.bot_id or not group_openid or not ref_idx:
        return
    _store(
        f"{bot.bot_id}:scene:{group_openid}",
        {
            "id": message_id,
            "msg_indices": [ref_idx],
            "sender_id": bot.bot_id,
            "sender_name": "",
            "images": _images(message),
            "texts": _texts(message),
        },
    )


def recent_reply(bot: BotClient, event: Event) -> dict[str, Any] | None:
    """按引用索引精确返回当前会话缓存的原消息。"""
    key = _key(bot, event)
    reply_idx = _reply_index(event)
    if not key or not reply_idx:
        return None
    return next(
        (item for item in reversed(_recent.get(key, ())) if reply_idx in item.get("msg_indices", ())),
        None,
    )


def cached_message(item: dict[str, Any]) -> Message:
    """把缓存条目还原成核心消息，供引用快照和图片读取共用。"""
    segments = [MessageSegment.text(text) for text in item.get("texts") or []]
    segments.extend(MessageSegment.image(source) for source in item.get("images") or [])
    return Message(segments)
