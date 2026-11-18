"""gsuid_core WebSocket 协议模型，只使用标准库 json 和 dataclass。"""

from __future__ import annotations

import json
from typing import Any, Literal
from dataclasses import field, dataclass

USER_TYPES = {"group", "direct", "channel", "sub_channel"}


@dataclass(slots=True)
class Message:
    """统一消息段。"""

    type: str | None = None
    data: Any = None


@dataclass(slots=True)
class MessageReceive:
    """适配器上行到 gsuid_core 的事件。"""

    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: str | None = None
    user_id: str = ""
    sender: dict[str, Any] = field(default_factory=dict)
    user_pm: int = 6
    content: list[Message] = field(default_factory=list)


@dataclass(slots=True)
class MessageSend:
    """gsuid_core 下发到适配器的发送或控制帧。"""

    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    target_type: str | None = None
    target_id: str | None = None
    content: list[Message] | None = None
    echo: str | None = None


def _text(value: Any, *, default: str = "") -> str:
    """协议标识统一为字符串，拒绝布尔值和不可序列化对象。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("协议字段必须为字符串")
    return str(value)


def decode_send(raw: str | bytes) -> MessageSend:
    """解析一帧下行 JSON；结构错误由调用方记录并丢弃该帧。"""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("gsuid_core 下行帧必须为对象")
    content_data = data.get("content")
    content = None
    if content_data is not None:
        if not isinstance(content_data, list):
            raise ValueError("gsuid_core content 必须为数组或 null")
        content = []
        for item in content_data:
            if not isinstance(item, dict):
                raise ValueError("gsuid_core 消息段必须为对象")
            kind = item.get("type")
            if kind is not None and not isinstance(kind, str):
                raise ValueError("gsuid_core 消息段类型必须为字符串")
            content.append(Message(kind, item.get("data")))
    echo = data.get("echo")
    return MessageSend(
        bot_id=_text(data.get("bot_id"), default="Bot"),
        bot_self_id=_text(data.get("bot_self_id")),
        msg_id=_text(data.get("msg_id")),
        target_type=_text(data.get("target_type")) or None,
        target_id=_text(data.get("target_id")) or None,
        content=content,
        echo=None if echo is None else _text(echo),
    )


def _message_payload(message: Message) -> dict[str, Any]:
    return {"type": message.type, "data": message.data}


def encode_receive(message: MessageReceive) -> bytes:
    """编码上行事件，保持字段名与 gsuid_core 的 Struct 一致。

    gsuid_core 的 WS 端只调用 receive_bytes()，上行必须是二进制帧；
    返回 str 会被 websockets 发成 Text 帧，导致服务端 KeyError: 'bytes'。
    """
    payload = {
        "bot_id": message.bot_id,
        "bot_self_id": message.bot_self_id,
        "msg_id": message.msg_id,
        "user_type": message.user_type,
        "group_id": message.group_id,
        "user_id": message.user_id,
        "sender": message.sender,
        "user_pm": message.user_pm,
        "content": [_message_payload(item) for item in message.content],
    }
    # 紧凑分隔符与原版 msgspec.json.encode 的输出保持一致
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def recall_receipt(message: MessageSend, recall_id: str | list[str] | None) -> MessageReceive:
    """构造 recall_message_id 回执；发送失败或无 id 时也必须回执。"""
    return MessageReceive(
        bot_id=message.bot_id,
        bot_self_id=message.bot_self_id,
        user_id="",
        content=[Message("recall_message_id", {"echo": message.echo, "id": recall_id})],
    )
