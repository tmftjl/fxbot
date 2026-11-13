"""OneBot V12 身份、消息和动作编解码。"""

from __future__ import annotations

import json
import math
from typing import Any
from datetime import datetime, timezone

from ...core import Event, Message, ReplyTarget, SessionTarget, MessageSegment, UnsupportedCapability


def _text(value: Any) -> str:
    """协议身份必须是非空字符串，不能从其他类型推测。"""
    if not isinstance(value, str) or not value:
        raise ValueError("协议身份必须是非空字符串")
    return value


def _json_copy(value: Any) -> Any:
    """复制严格 JSON 数据，不保留传输对象引用。"""
    return json.loads(json.dumps(value, allow_nan=False))


def parse_message(value: Any) -> Message:
    """读取规范消息数组，并保留未知消息段的协议命名空间。"""
    if not isinstance(value, list):
        raise ValueError("消息必须是数组")
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
            raise ValueError("消息段必须包含类型和数据对象")
        kind, data = _text(item.get("type")), _json_copy(item["data"])
        if kind == "text":
            if not isinstance(data.get("text"), str):
                raise ValueError("文本消息缺少文本")
            segment = MessageSegment.text(data["text"])
        elif kind == "mention":
            segment = MessageSegment.mention(_text(data.get("user_id")))
        elif kind == "mention_all":
            segment = MessageSegment("mention_all")
        elif kind == "reply":
            _text(data.get("message_id"))
            if "user_id" in data:
                _text(data["user_id"])
            segment = MessageSegment("reply", data)
        elif kind in {"image", "voice", "audio", "video", "file"}:
            _text(data.get("file_id"))
            segment = MessageSegment(kind, data)
        else:
            segment = MessageSegment(f"onebot12:{kind}", data)
        result.append(segment)
    return Message(result)


def encode_message(message: Message) -> list[dict[str, Any]]:
    """编码已支持的消息段，媒体必须使用已上传的文件 ID。"""
    result = []
    for segment in message.segments:
        kind, data = segment.type, _json_copy(segment.data)
        if kind == "text":
            if not isinstance(data.get("text"), str):
                raise ValueError("文本消息缺少文本")
        elif kind == "mention":
            data = {"user_id": _text(data.get("user_id"))}
        elif kind == "reply":
            _text(data.get("message_id"))
        elif kind in {"image", "voice", "audio", "video", "file"}:
            _text(data.get("file_id"))
        elif kind == "mention_all":
            data = {}
        else:
            raise UnsupportedCapability("不支持此出站消息段")
        result.append({"type": kind, "data": data})
    return result


def parse_event(payload: dict[str, Any], *, bot_id: str, platform: str) -> Event:
    """校验自身标识并解析私聊、群聊或通知事件。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("self"), dict):
        raise ValueError("事件缺少自身身份")
    identity = payload["self"]
    if identity.get("platform") != platform or identity.get("user_id") != bot_id:
        raise ValueError("连接与事件身份不一致")
    event_id = _text(payload.get("id"))
    timestamp = payload.get("time")
    if type(timestamp) not in {int, float} or not math.isfinite(timestamp) or not 0 <= timestamp <= 253402300799:
        raise ValueError("事件时间必须为有限非负数")
    kind = _text(payload.get("detail_type"))
    subtype = payload.get("sub_type")
    if not isinstance(subtype, str):
        raise ValueError("事件子类型必须为字符串")
    user_id = _text(payload.get("user_id"))
    protocol = f"onebot12.{platform}"
    metadata = {"time": timestamp, "detail_type": kind, "sub_type": subtype}
    if payload.get("type") == "message":
        if kind not in {"group", "private"}:
            raise UnsupportedCapability("不支持此会话类型")
        target_id = _text(payload.get("group_id")) if kind == "group" else user_id
        original = parse_message(payload.get("message"))
        message = parse_message(payload.get("message"))
        reply_data = next((item.data for item in message.segments if item.type == "reply"), None)
        reply = ReplyTarget(reply_data["message_id"], reply_data.get("user_id")) if reply_data is not None else None
        to_me = kind == "private" or bot_id in original.mentions()
        if kind == "group":
            while message.segments and message.segments[0].type == "mention":
                if message.segments[0].data["user_id"] != bot_id:
                    break
                message.segments.pop(0)
        return Event(
            event_id,
            _text(payload.get("message_id")),
            "message",
            protocol,
            bot_id,
            user_id,
            "",
            SessionTarget(protocol, bot_id, kind, target_id),
            message=message,
            reply=reply,
            to_me=to_me,
            metadata=metadata,
            original_message=original,
            occurred_at=datetime.fromtimestamp(timestamp, timezone.utc),
        )
    if payload.get("type") != "notice":
        raise UnsupportedCapability("不支持此事件类型")
    names = {
        "group_member_increase": "member_joined",
        "group_member_decrease": "member_left",
        "group_message_delete": "message_deleted",
        "private_message_delete": "message_deleted",
        "friend_increase": "friend_added",
        "friend_decrease": "friend_removed",
    }
    if kind not in names:
        raise UnsupportedCapability("不支持此通知类型")
    group_id = _text(payload.get("group_id")) if kind.startswith("group_") else None
    if "operator_id" in payload:
        metadata["operator_id"] = _text(payload["operator_id"])
    return Event(
        event_id,
        _text(payload.get("message_id")) if kind.endswith("message_delete") else None,
        names[kind],
        protocol,
        bot_id,
        user_id,
        "",
        SessionTarget(protocol, bot_id, "group" if group_id else "private", group_id or user_id),
        metadata=metadata,
        occurred_at=datetime.fromtimestamp(timestamp, timezone.utc),
    )
