"""QQ 官方回调事件到核心事件的确定性转换。"""

from __future__ import annotations

import re
import html
from datetime import datetime
from urllib.parse import urlsplit

from ...core import Event, Message, ReplyTarget, SessionTarget, MessageSegment, UnsupportedCapability

MESSAGE_TYPES = {
    "C2C_MESSAGE_CREATE",
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
    "AT_MESSAGE_CREATE",
    "MESSAGE_CREATE",
    "DIRECT_MESSAGE_CREATE",
}


def identifier(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", value):
        raise ValueError("QQ 身份必须是有效的官方 ID/OpenID 字符串")
    return value


def opaque_id(value) -> str:
    """消息/事件 ID 是不透明字符串，不能按 QQ 号或 URL 路径猜格式。"""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
    ):
        raise ValueError("QQ 消息或事件 ID 无效")
    return value


def media_url(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("QQ 媒体地址缺失")
    value = value if "://" in value else "https:" + value if value.startswith("//") else "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("QQ 媒体地址必须是 HTTPS")
    return value


def parse_message(data: dict) -> Message:
    content = data.get("content", "")
    if not isinstance(content, str):
        raise ValueError("QQ 消息文本必须为字符串")
    segments = []
    offset = 0
    for match in re.finditer(r"<@!?([A-Za-z0-9_.-]+)>", content):
        if match.start() > offset:
            segments.append(MessageSegment.text(html.unescape(content[offset : match.start()])))
        segments.append(MessageSegment.mention(match[1]))
        offset = match.end()
    if offset < len(content):
        segments.append(MessageSegment.text(html.unescape(content[offset:])))
    attachments = data.get("attachments")
    if attachments is None:
        attachments = []
    if not isinstance(attachments, list):
        raise ValueError("QQ attachments 必须为数组")
    for item in attachments:
        if not isinstance(item, dict) or not isinstance(item.get("content_type"), str):
            raise ValueError("QQ 媒体信息格式无效")
        mime = item["content_type"]
        kind = (
            "image"
            if mime.startswith("image/")
            else "voice"
            if mime.startswith("audio/")
            else "video"
            if mime.startswith("video/")
            else "file"
        )
        url = media_url(item.get("url"))
        segments.append(MessageSegment(kind, {"file": url, "url": url, "name": item.get("filename", "")}))
    return Message(segments)


def parse_event(payload: dict, *, bot_id: str, bot_user_id: str = "") -> Event:
    if not isinstance(payload, dict) or payload.get("op") != 0 or type(payload.get("op")) is not int:
        raise ValueError("QQ 事件必须为 Dispatch 帧")
    event_type, data = payload.get("t"), payload.get("d")
    if event_type not in MESSAGE_TYPES:
        raise UnsupportedCapability("尚未支持此 QQ 官方事件")
    if not isinstance(data, dict) or not isinstance(data.get("author"), dict):
        raise ValueError("QQ 消息缺少 author")
    author = data["author"]
    if author.get("bot") is True:
        raise UnsupportedCapability("忽略其他机器人发送的消息")
    if event_type == "C2C_MESSAGE_CREATE":
        user = identifier(author.get("user_openid"))
        target = SessionTarget("qq", bot_id, "private", user)
    elif event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
        user = identifier(author.get("member_openid"))
        target = SessionTarget("qq", bot_id, "group", identifier(data.get("group_openid")))
    else:
        user = identifier(author.get("id"))
        channel, guild = identifier(data.get("channel_id")), identifier(data.get("guild_id"))
        target = (
            SessionTarget("qq", bot_id, "private", f"dms/{guild}/{user}")
            if event_type == "DIRECT_MESSAGE_CREATE"
            else SessionTarget("qq", bot_id, "channel", channel)
        )
    message_id = opaque_id(data.get("id"))
    timestamp = data.get("timestamp")
    if not isinstance(timestamp, str):
        raise ValueError("QQ 消息缺少 timestamp")
    occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if occurred_at.tzinfo is None:
        raise ValueError("QQ 消息时间必须带时区")
    original = parse_message(data)
    message = Message([MessageSegment(part.type, dict(part.data)) for part in original.segments])
    to_me = event_type in {
        "C2C_MESSAGE_CREATE",
        "GROUP_AT_MESSAGE_CREATE",
        "AT_MESSAGE_CREATE",
        "DIRECT_MESSAGE_CREATE",
    }
    if bot_user_id:
        to_me = to_me or bot_user_id in message.mentions()
        for part in message.segments:
            if part.type == "mention" and part.data["user_id"] == bot_user_id:
                part.data["user_id"] = bot_id
        while (
            message.segments and message.segments[0].type == "mention" and message.segments[0].data["user_id"] == bot_id
        ):
            message.segments.pop(0)
    mentions = data.get("mentions")
    if mentions is None:
        mentions = []
    if not isinstance(mentions, list):
        raise ValueError("QQ mentions 必须是数组")
    if target.kind == "group":
        identities = {}
        for mention in mentions:
            if not isinstance(mention, dict):
                raise ValueError("QQ 提及记录无效")
            if mention.get("scope") == "single":
                raw_id = identifier(mention.get("id"))
                own = mention.get("is_you") is True
                identities[raw_id] = bot_id if own else identifier(mention.get("member_openid"))
                to_me = to_me or own
        for part in message.segments:
            if part.type == "mention":
                part.data["user_id"] = identities.get(part.data["user_id"], part.data["user_id"])
        while (
            message.segments and message.segments[0].type == "mention" and message.segments[0].data["user_id"] == bot_id
        ):
            message.segments.pop(0)
    if message.segments and message.segments[0].type == "text":
        message.segments[0].data["text"] = message.segments[0].text_value().lstrip()
    role = author.get("member_role")
    if role is not None and role not in {"member", "admin", "owner"}:
        raise ValueError("QQ 群成员角色无效")
    metadata = {"qq_event_type": event_type}
    if target.kind == "group" and role is not None:
        metadata["sender_role"] = role
    if event_type in {"AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}:
        metadata.update(guild_id=guild, channel_id=channel)
    reference = data.get("message_reference")
    reply = None
    if reference is not None:
        if not isinstance(reference, dict):
            raise ValueError("QQ 引用信息无效")
        reply = ReplyTarget(opaque_id(reference.get("message_id")))
    event_id = payload.get("id")
    if event_id is not None:
        event_id = opaque_id(event_id)
    username = author.get("username") or ""
    if not isinstance(username, str):
        raise ValueError("QQ 用户名必须为字符串")
    return Event(
        event_id,
        message_id,
        "message",
        "qq",
        bot_id,
        user,
        username,
        target,
        message,
        reply,
        metadata,
        to_me,
        original,
        occurred_at,
    )
