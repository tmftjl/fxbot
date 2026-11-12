"""OneBot V11 消息事件解析和发送参数编码。"""

from __future__ import annotations

import json
import math
from typing import Any
from collections.abc import Callable, Awaitable

from ..types import Event, Message, ReplyTarget, SessionTarget, MessageSegment, UnsupportedCapability

ActionSender = Callable[[str, dict[str, Any]], Awaitable[Any]]


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
    if not isinstance(value, list):
        raise UnsupportedCapability("只支持消息段数组格式")
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
            segment = MessageSegment("audio" if kind == "record" else kind, data)
        else:
            segment = MessageSegment(f"onebot11:{kind}", data)
        segments.append(segment)
    return Message(segments)


def parse_event(payload: dict[str, Any], *, bot_id: str) -> Event:
    """解析私聊或群消息，校验连接身份，不发起网络查询。"""
    if not isinstance(payload, dict):
        raise ValueError("事件必须为字典")
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
    if type(timestamp) is not int or timestamp < 0:
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
    )


def encode_message(message: Message) -> list[dict[str, Any]]:
    """将可发送的核心消息编码为协议数组，拒绝未知能力。"""
    result = []
    for segment in message.segments:
        kind, data = segment.type, _copy(segment.data)
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
        elif kind in {"audio", "image", "video"}:
            if not isinstance(data.get("file"), str) or not data["file"]:
                raise ValueError("媒体段必须包含文件来源")
            kind = "record" if kind == "audio" else kind
        else:
            raise UnsupportedCapability("不支持此出站消息段")
        result.append({"type": kind, "data": data})
    json.dumps(result, allow_nan=False)
    return result


class OneBot11Client:
    """将核心发送请求交给显式注入的协议传输函数。"""

    platform = "onebot11"

    def __init__(self, bot_id: str, sender: ActionSender) -> None:
        """绑定连接身份和动作传输函数。"""
        self.bot_id = _id(bot_id)
        self._sender = sender

    async def call(self, action: str, **params: Any) -> Any:
        """发送协议动作，网络鉴权与响应关联由传输层负责。"""
        if not isinstance(action, str) or not action:
            raise ValueError("动作名称不能为空")
        data = _copy(params)
        json.dumps(data, allow_nan=False)
        return await self._sender(action, data)

    async def send(self, target: SessionTarget, message: Message) -> Any:
        """向同一连接的私聊或群目标发送已编码消息。"""
        if (target.platform, target.bot_id) != (self.platform, self.bot_id):
            raise ValueError("连接与发送目标身份不一致")
        if target.kind not in {"private", "group"}:
            raise UnsupportedCapability("不支持此发送目标")
        key = "group_id" if target.kind == "group" else "user_id"
        action = "send_group_msg" if target.kind == "group" else "send_private_msg"
        return await self.call(action, **{key: int(_id(target.target_id)), "message": encode_message(message)})
