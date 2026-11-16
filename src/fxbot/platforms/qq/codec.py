"""QQ 官方回调事件到核心事件的确定性转换。"""

from __future__ import annotations

import re
import html
from copy import deepcopy
from typing import Any
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlsplit

from ...core import Event, Message, ReplyTarget, SessionTarget, MessageSegment, UnsupportedCapability

MESSAGE_TYPES = {
    "C2C_MESSAGE_CREATE",
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
    "AT_MESSAGE_CREATE",
    "MESSAGE_CREATE",
    "DIRECT_MESSAGE_CREATE",
}
NOTICE_TYPES = {
    "FRIEND_ADD": "friend_added",
    "FRIEND_DEL": "friend_removed",
    "C2C_MSG_REJECT": "private_messages_rejected",
    "C2C_MSG_RECEIVE": "private_messages_received",
    "GROUP_ADD_ROBOT": "bot_group_joined",
    "GROUP_DEL_ROBOT": "bot_group_left",
    "GROUP_MSG_REJECT": "group_messages_rejected",
    "GROUP_MSG_RECEIVE": "group_messages_received",
    "GROUP_MEMBER_ADD": "member_joined",
    "GROUP_MEMBER_REMOVE": "member_left",
    "GROUP_JOIN_REQUEST": "group_join_requested",
    "INTERACTION_CREATE": "interaction",
    "MESSAGE_AUDIT_PASS": "message_audit_passed",
    "MESSAGE_AUDIT_REJECT": "message_audit_rejected",
}


def _time(value: object) -> datetime:
    if type(value) is int:
        if not 0 <= value <= 253402300799:
            raise ValueError("QQ 事件 Unix 时间戳超出有效范围")
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=value)
    if not isinstance(value, str):
        raise ValueError("QQ 事件时间必须是 Unix 秒整数或带时区的字符串")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("QQ 事件时间必须包含时区")
    return value


def _notice(payload: dict, bot_id: str) -> Event:
    kind, data = payload["t"], payload["d"]
    if not isinstance(data, dict):
        raise ValueError("QQ 通知必须包含 d 对象")
    if kind == "INTERACTION_CREATE":
        if type(data.get("chat_type")) is not int:
            raise ValueError("QQ 交互 chat_type 必须为整数")
        if data.get("application_id") != bot_id:
            raise ValueError("QQ 交互不属于当前 AppID")
        if data.get("chat_type") == 1:
            user = identifier(data.get("group_member_openid"))
            target = SessionTarget("qq", bot_id, "group", identifier(data.get("group_openid")))
        elif data.get("chat_type") == 2:
            user = identifier(data.get("user_openid"))
            target = SessionTarget("qq", bot_id, "private", user)
        else:
            raise UnsupportedCapability("本次对齐不包含频道交互")
        timestamp = data.get("timestamp")
        interaction_id = opaque_id(data.get("id"))
        metadata: dict[str, Any] = {"interaction_id": interaction_id}
    elif kind.startswith("MESSAGE_AUDIT_"):
        if data.get("group_openid"):
            target = SessionTarget("qq", bot_id, "group", identifier(data["group_openid"]))
            user = identifier(data["user_openid"]) if data.get("user_openid") else bot_id
        elif data.get("user_openid"):
            user = identifier(data["user_openid"])
            target = SessionTarget("qq", bot_id, "private", user)
        else:
            raise UnsupportedCapability("本次对齐不包含频道审核事件")
        timestamp = data.get("audit_time")
        metadata = {"audit_id": opaque_id(data.get("audit_id"))}
    elif kind.startswith("GROUP_"):
        user = identifier(
            data.get("op_member_openid")
            if kind in {"GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_REJECT", "GROUP_MSG_RECEIVE"}
            else data.get("member_openid")
        )
        target = SessionTarget("qq", bot_id, "group", identifier(data.get("group_openid")))
        timestamp = data.get("apply_at") if kind == "GROUP_JOIN_REQUEST" else data.get("timestamp")
        metadata = {}
        if kind == "GROUP_JOIN_REQUEST":
            metadata["join_request_id"] = opaque_id(data.get("join_request_id"))
        if data.get("op_member_openid"):
            metadata["operator_id"] = identifier(data["op_member_openid"])
    else:
        user = identifier(data.get("openid"))
        target = SessionTarget("qq", bot_id, "private", user)
        timestamp = data.get("timestamp")
        metadata = {}
    metadata.update(qq_event_type=kind, qq_data=deepcopy(data))
    username = data.get("username") or ""
    if not isinstance(username, str):
        raise ValueError("QQ 通知用户名必须是字符串")
    return Event(
        opaque_id(payload["id"]) if payload.get("id") is not None else None,
        None,
        NOTICE_TYPES[kind],
        "qq",
        bot_id,
        user,
        username,
        target,
        metadata=metadata,
        to_me=True,
        occurred_at=_time(timestamp),
    )


def identifier(value) -> str:
    if not isinstance(value, str) or value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", value):
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


def api_path(*parts: str) -> str:
    """所有官方 API 路径共用段校验，消息 ID 仍允许不透明字符。"""
    for part in parts:
        opaque_id(part)
        if part in {".", ".."}:
            raise ValueError("QQ 接口路径不能包含相对目录")
    return "/" + "/".join(quote(part, safe="") for part in parts)


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
    pattern = (
        r'<@!?([A-Za-z0-9_.-]+)>|<qqbot-at-user id="([A-Za-z0-9_.-]+)"\s*/>'
        r'|<emoji:([A-Za-z0-9_.-]+)>|@everyone|<faceType=\d+,faceId="(\d+)",ext="[\w=]+">'
    )
    for match in re.finditer(pattern, content):
        if match.start() > offset:
            segments.append(MessageSegment.text(html.unescape(content[offset : match.start()])))
        if match[1] or match[2]:
            segments.append(MessageSegment.mention(match[1] or match[2]))
        elif match[3] or match[4]:
            segments.append(MessageSegment("qq:emoji", {"id": match[3] or match[4]}))
        else:
            segments.append(MessageSegment("mention_all"))
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
        if item.get("url") is None:
            continue
        url = media_url(item.get("url"))
        segments.append(MessageSegment(kind, {"file": url, "url": url, "name": item.get("filename", "")}))
    return Message(segments)


def parse_event(payload: dict, *, bot_id: str, bot_user_id: str = "") -> Event:
    if not isinstance(payload, dict) or payload.get("op") != 0 or type(payload.get("op")) is not int:
        raise ValueError("QQ 事件必须为 Dispatch 帧")
    event_type, data = payload.get("t"), payload.get("d")
    if event_type in NOTICE_TYPES:
        return _notice(payload, bot_id)
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
        present = set(message.mentions())
        for mention in mentions:
            if mention.get("scope") == "single":
                member = identities[mention["id"]]
                if member not in present:
                    if member == bot_id:
                        message.segments.insert(0, MessageSegment.mention(member))
                    else:
                        message.segments.append(MessageSegment.mention(member, mention.get("username") or ""))
                    present.add(member)
            elif mention.get("scope") == "all":
                to_me = True
                if not any(part.type == "mention_all" for part in message.segments):
                    message.segments.append(MessageSegment("mention_all"))
        while (
            message.segments and message.segments[0].type == "mention" and message.segments[0].data["user_id"] == bot_id
        ):
            message.segments.pop(0)
    if message.segments and message.segments[0].type == "text":
        message.segments[0].data["text"] = message.segments[0].text_value().lstrip()
    role = author.get("member_role")
    if role is not None and role not in {"member", "admin", "owner"}:
        raise ValueError("QQ 群成员角色无效")
    metadata = {"qq_event_type": event_type, "qq_data": deepcopy(data)}
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
    if data.get("message_type") == 103:
        scene = data.get("message_scene") or {}
        elements = data.get("msg_elements") or []
        ext = scene.get("ext", [])
        if not isinstance(ext, list) or not isinstance(elements, list):
            raise ValueError("QQ 引用消息结构无效")
        reference_index = next(
            (
                value.removeprefix("ref_msg_idx=")
                for value in ext
                if isinstance(value, str) and value.startswith("ref_msg_idx=")
            ),
            None,
        )
        quoted = (
            next((element for element in elements if element.get("msg_idx") == reference_index), None)
            if reference_index
            else None
        )
        if quoted is not None:
            metadata["qq_reply"] = deepcopy(quoted)
            reply_author = quoted.get("author") or {}
            author_key = "member_openid" if target.kind == "group" else "user_openid"
            reply = ReplyTarget(
                opaque_id(reference_index),
                identifier(reply_author[author_key]) if reply_author.get(author_key) else None,
                reply_author.get("username") or "",
            )
            if bot_user_id and reply_author.get("id") == bot_user_id:
                to_me = True
            if target.kind == "group":
                index = 0
                while (
                    index < len(message.segments)
                    and message.segments[index].type == "text"
                    and not message.segments[index].text_value().strip()
                ):
                    index += 1
                if index < len(message.segments) and message.segments[index].type == "mention":
                    mentioned = message.segments[index].data["user_id"]
                    authors = {
                        value for key in ("id", "member_openid", "user_openid") if (value := reply_author.get(key))
                    }
                    known_mentions = {
                        entry.get("member_openid") for entry in mentions if entry.get("scope") == "single"
                    }
                    if mentioned in (authors if authors else known_mentions):
                        del message.segments[: index + 1]
                        if message.segments and message.segments[0].type == "text":
                            message.segments[0].data["text"] = message.segments[0].text_value().lstrip()
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
