"""OneBot V11 消息事件解析和发送参数编码。"""

from __future__ import annotations

import re
import json
import math
import base64
from typing import Any
from datetime import datetime, timezone

from ...core.types import Event, Message, ReplyTarget, SessionTarget, MessageSegment, UnsupportedCapability

_CQ_SEGMENT = re.compile(r"\[CQ:([A-Za-z0-9_-]+)((?:,[^\[\]]*)?)\]")


def _unescape(value: str, *, parameter: bool = False) -> str:
    """按协议顺序还原一层转义，不能把转义后的文本重新当作消息段。"""
    if parameter:
        value = value.replace("&#44;", ",")
    return value.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def _parse_cq(value: str) -> list[dict[str, Any]]:
    """把显式 CQ 字符串解析成数组，不递归解析解码后的文本。"""
    segments = []
    offset = 0
    for match in _CQ_SEGMENT.finditer(value):
        if match.start() > offset:
            segments.append({"type": "text", "data": {"text": _unescape(value[offset : match.start()])}})
        data = {}
        for item in match.group(2).lstrip(",").split(",") if match.group(2) else ():
            key, separator, content = item.partition("=")
            if not separator or not key or key in data:
                raise ValueError("CQ 参数必须具有唯一名称和值")
            data[key] = _unescape(content, parameter=True)
        segments.append({"type": match.group(1), "data": data})
        offset = match.end()
    if offset < len(value):
        segments.append({"type": "text", "data": {"text": _unescape(value[offset:])}})
    return segments


def _id(value: Any, *, positive: bool = True) -> str:
    """规范化十进制整数 ID，拒绝布尔值和空目标。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("协议 ID 必须为十进制整数")
    text = str(value)
    digits = text[1:] if text.startswith("-") else text
    if not digits or not digits.isascii() or not digits.isdecimal():
        raise ValueError("协议 ID 必须为十进制整数")
    number = int(text)
    if positive and number <= 0:
        raise ValueError("目标 ID 必须为正整数")
    return str(number)


def _copy(value: Any) -> Any:
    """复制 JSON 数据，不允许嵌入客户端或其他运行时对象。"""
    if type(value) is float and not math.isfinite(value):
        raise ValueError("协议数字必须为有限值")
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, list):
        return [_copy(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _copy(item) for key, item in value.items()}
    raise ValueError("协议数据必须为 JSON 类型")


def parse_message(value: Any) -> Message:
    """解析消息段数组，未知类型保留在平台命名空间。"""
    if isinstance(value, str):
        value = _parse_cq(value)
    if not isinstance(value, list):
        raise ValueError("消息必须是数组或 CQ 字符串")
    segments = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or not item["type"]:
            raise ValueError("消息段缺少有效类型")
        kind, data = item["type"], _copy(item.get("data"))
        if not isinstance(data, dict):
            raise ValueError("消息段数据必须为字典")
        if kind == "at":
            segment = (
                MessageSegment("mention_all")
                if data.get("qq") == "all"
                else MessageSegment.mention(_id(data.get("qq")))
            )
        elif kind == "reply":
            segment = MessageSegment("reply", {"message_id": _id(data.get("id"), positive=False)})
        elif kind == "text":
            if not isinstance(data.get("text"), str):
                raise ValueError("文本段必须包含文本")
            segment = MessageSegment.text(data["text"])
        elif kind in {"image", "video", "record"}:
            if kind == "image":
                data["is_emoji"] = str(data.get("sub_type", "0")) == "1"
            segment = MessageSegment("voice" if kind == "record" else kind, data)
        elif kind == "forward":
            if not isinstance(data.get("id"), str) or not data["id"]:
                raise ValueError("转发段必须包含明确的 ID")
            segment = MessageSegment("forward", {"id": data["id"]})
        else:
            segment = MessageSegment(f"onebot11:{kind}", data)
        segments.append(segment)
    return Message(segments)


def parse_event(payload: dict[str, Any], *, bot_id: str) -> Event:
    """解析私聊或群消息，校验连接身份，不发起网络查询。"""
    if not isinstance(payload, dict):
        raise ValueError("事件必须为字典")
    if payload.get("post_type") == "notice":
        return parse_notice(payload, bot_id=bot_id)
    if payload.get("post_type") != "message":
        raise UnsupportedCapability("只支持消息事件")
    identity = _id(bot_id)
    if _id(payload.get("self_id")) != identity:
        raise ValueError("连接与事件身份不一致")
    kind = payload.get("message_type")
    if kind not in {"private", "group"}:
        raise UnsupportedCapability("不支持此会话类型")
    if kind == "group" and (payload.get("sub_type") == "anonymous" or payload.get("anonymous") is not None):
        raise UnsupportedCapability("不支持匿名会话身份")
    user_id = _id(payload.get("user_id"))
    target_id = _id(payload.get("group_id")) if kind == "group" else user_id
    original = parse_message(payload.get("message"))
    message = parse_message(payload.get("message"))
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        raise ValueError("缺少发送者信息")
    sender_id = sender.get("user_id")
    if sender_id is not None and _id(sender_id) != user_id:
        raise ValueError("发送者身份不一致")
    name = sender.get("card") or sender.get("nickname") or ""
    if not isinstance(name, str):
        raise ValueError("发送者名称必须为文本")
    timestamp = payload.get("time")
    if type(timestamp) is not int or not 0 <= timestamp <= 253402300799:
        raise ValueError("事件时间必须为非负整数")
    subtype = payload.get("sub_type")
    if not isinstance(subtype, str):
        raise ValueError("事件子类型必须为文本")
    reply = next(
        (ReplyTarget(segment.data["message_id"]) for segment in message.segments if segment.type == "reply"), None
    )
    to_me = kind == "private" or identity in message.mentions()
    role = sender.get("role")
    if kind == "group":
        # 命令识别使用去掉首尾自身提及的消息，完整消息仍单独保留。
        while message.segments and message.segments[0].type == "mention":
            if message.segments[0].data["user_id"] != identity:
                break
            message.segments.pop(0)
        while message.segments and message.segments[-1].type == "mention":
            if message.segments[-1].data["user_id"] != identity:
                break
            message.segments.pop()
    return Event(
        event_id=None,
        message_id=_id(payload.get("message_id"), positive=False),
        event_type="message",
        platform="onebot11",
        bot_id=identity,
        user_id=user_id,
        user_name=name,
        target=SessionTarget("onebot11", identity, kind, target_id),
        message=message,
        reply=reply,
        metadata={
            "time": timestamp,
            "sub_type": subtype,
            "sender_role": role if isinstance(role, str) and role in {"admin", "owner", "member"} else None,
        },
        to_me=to_me,
        original_message=original,
        occurred_at=datetime.fromtimestamp(timestamp, timezone.utc),
    )


def parse_notice(payload: dict[str, Any], *, bot_id: str) -> Event:
    """将已知通知规范化为核心事件，不查询远端数据。"""
    identity = _id(bot_id)
    if _id(payload.get("self_id")) != identity:
        raise ValueError("连接与事件身份不一致")
    notice = payload.get("notice_type")
    supported = {
        "group_increase": "member_joined",
        "group_decrease": "member_left",
        "group_recall": "message_deleted",
        "friend_recall": "message_deleted",
        "group_admin": "member_role_changed",
        "group_ban": "member_muted",
        "group_upload": "file_uploaded",
        "friend_add": "friend_added",
        "notify": "notification",
    }
    if notice not in supported:
        raise UnsupportedCapability("不支持此通知类型")
    user_id = _id(payload.get("user_id"), positive=notice != "group_ban")
    group_id = _id(payload["group_id"]) if "group_id" in payload else None
    if str(notice).startswith("group_") and group_id is None:
        raise ValueError("群通知缺少群号")
    timestamp = payload.get("time")
    if type(timestamp) is not int or not 0 <= timestamp <= 253402300799:
        raise ValueError("事件时间必须为非负整数")
    metadata = {"time": timestamp, "notice_type": notice, "sub_type": payload.get("sub_type", "")}
    for key in ("operator_id", "target_id"):
        if key in payload:
            metadata[key] = _id(payload[key])
    for key in ("duration", "file", "honor_type"):
        if key in payload:
            metadata[key] = _copy(payload[key])
    message_id = _id(payload.get("message_id"), positive=False) if notice in {"group_recall", "friend_recall"} else None
    return Event(
        None,
        message_id,
        supported[notice],
        "onebot11",
        identity,
        user_id,
        "",
        SessionTarget("onebot11", identity, "group" if group_id is not None else "private", group_id or user_id),
        metadata=metadata,
        occurred_at=datetime.fromtimestamp(timestamp, timezone.utc),
    )


def encode_message(message: Message) -> list[dict[str, Any]]:
    """将可发送的核心消息编码为协议数组，拒绝未知能力。"""
    result = []
    for segment in message.segments:
        kind = segment.type
        raw = dict(segment.data)
        if kind in {"image", "audio", "voice", "video"} and isinstance(raw.get("file"), bytes):
            raw["file"] = "base64://" + base64.b64encode(raw["file"]).decode("ascii")
        data = _copy(raw)
        if not isinstance(data, dict):
            raise ValueError("消息段数据必须为字典")
        if kind == "mention":
            kind, data = "at", {"qq": _id(data.get("user_id"))}
        elif kind == "mention_all":
            kind, data = "at", {"qq": "all"}
        elif kind == "reply":
            data = {"id": _id(data.get("message_id"), positive=False)}
        elif kind == "text":
            if not isinstance(data.get("text"), str):
                raise ValueError("文本段必须包含文本")
        elif kind in {"audio", "voice", "image", "video"}:
            if not isinstance(data.get("file"), str) or not data["file"]:
                raise ValueError("媒体段必须包含文件来源")
            kind = "record" if kind in {"audio", "voice"} else kind
        else:
            raise UnsupportedCapability("不支持此出站消息段")
        result.append({"type": kind, "data": data})
    json.dumps(result, allow_nan=False)
    return result
