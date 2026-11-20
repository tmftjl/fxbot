"""QQ 官方回调事件到核心事件的确定性转换。"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from yarl import URL
from loguru import logger
from pydantic import TypeAdapter

from ...core import Event, Message, ReplyTarget, SessionTarget, MessageSegment
from .event_models import EVENT_CLASSES

_PATH_ROOT = URL("https://api.bot.qq.com")

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
    value = TypeAdapter(datetime).validate_python(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _occurred_at(value: object) -> datetime:
    # QQ 消息和交互中的时间字段保留原始字符串；无法解析时核心事件使用接收时间。
    try:
        return _time(value)
    except ValueError:
        return datetime.now(timezone.utc)


def _notice(payload: dict, bot_id: str, raw_data: dict) -> Event:
    kind, data = payload["t"], payload.get("d")
    if not isinstance(data, dict):
        raise ValueError("QQ 通知必须包含 d 对象")
    if kind == "INTERACTION_CREATE":
        if type(data.get("chat_type")) is not int:
            raise ValueError("QQ 交互 chat_type 必须为整数")
        if data.get("chat_type") == 0:
            user = data["data"]["resolved"].get("user_id") or ""
            target = SessionTarget("qq", bot_id, "channel", data.get("channel_id") or "")
        elif data.get("chat_type") == 1:
            user = data.get("group_member_openid") or ""
            target = SessionTarget("qq", bot_id, "group", data.get("group_openid") or "")
        elif data.get("chat_type") == 2:
            user = data.get("user_openid") or ""
            target = SessionTarget("qq", bot_id, "private", user)
        else:
            user = ""
            target = SessionTarget("qq", bot_id, "guild", data.get("guild_id") or "")
        timestamp = data.get("timestamp")
        interaction_id = data.get("id")
        metadata: dict[str, Any] = {"interaction_id": interaction_id}
    elif kind.startswith("MESSAGE_AUDIT_"):
        if data.get("group_openid"):
            target = SessionTarget("qq", bot_id, "group", (data["group_openid"]))
            user = (data["user_openid"]) if data.get("user_openid") else bot_id
        elif data.get("user_openid"):
            user = data["user_openid"]
            target = SessionTarget("qq", bot_id, "private", user)
        else:
            user = ""
            target = SessionTarget("qq", bot_id, "channel", data.get("channel_id") or "")
        timestamp = data.get("audit_time")
        metadata = {"audit_id": (data.get("audit_id"))}
    elif kind.startswith("GROUP_"):
        user = (
            data.get("op_member_openid")
            if kind in {"GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_REJECT", "GROUP_MSG_RECEIVE"}
            else data.get("member_openid")
        )
        target = SessionTarget("qq", bot_id, "group", (data.get("group_openid")))
        timestamp = data.get("apply_at") if kind == "GROUP_JOIN_REQUEST" else data.get("timestamp")
        metadata = {}
        if kind == "GROUP_JOIN_REQUEST":
            metadata["join_request_id"] = data.get("join_request_id")
        if data.get("op_member_openid"):
            metadata["operator_id"] = data["op_member_openid"]
    else:
        user = data.get("openid")
        target = SessionTarget("qq", bot_id, "private", user)
        timestamp = data.get("timestamp")
        metadata = {}
    metadata.update(qq_event_type=kind, qq_data=deepcopy(raw_data))
    username = data.get("username") or ""
    if not isinstance(username, str):
        raise ValueError("QQ 通知用户名必须是字符串")
    return Event(
        (payload["id"]) if payload.get("id") is not None else None,
        None,
        NOTICE_TYPES[kind],
        "qq",
        bot_id,
        user,
        username,
        target,
        metadata=metadata,
        to_me=False,
        occurred_at=_occurred_at(timestamp),
    )


def api_path(*parts: str) -> str:
    """按官方 URL 路径拼接，不额外限制官方 ID 的内容。"""
    return _PATH_ROOT.joinpath(*parts).raw_path


def media_url(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("QQ 媒体地址缺失")
    return value if urlsplit(value).hostname else "https://" + value


def parse_message(data: dict, *, guild: bool = False) -> Message:
    content = data.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ValueError("QQ 消息文本必须为字符串")
    segments = [MessageSegment("mention_all")] if guild and data.get("mention_everyone") else []
    content = re.sub(r"<qqbot-at-everyone\s/>", "", content.replace("@everyone", ""))
    offset = 0
    pattern = (
        r'<@!?(\w+)>|<qqbot-at-user id="(\w+)"\s/>'
        r'|<emoji:(\w+)>|<faceType=\d+,faceId="(\d+)",ext="[\w=]+">|<#!?(\w+)>'
    )
    for match in re.finditer(pattern, content):
        if match.start() > offset:
            segments.append(MessageSegment.text(_unescape(content[offset : match.start()])))
        if match[1] or match[2]:
            segments.append(
                MessageSegment("mention_all") if match[1] == "all" else MessageSegment.mention(match[1] or match[2])
            )
        elif match[4] and match[4] != "0":
            segments.append(MessageSegment("qq:emoji", {"id": match[4]}))
        elif match[5]:
            segments.append(MessageSegment("qq:mention_channel", {"channel_id": match[5]}))
        offset = match.end()
    if offset < len(content):
        segments.append(MessageSegment.text(_unescape(content[offset:])))
    attachments = data.get("attachments")
    if attachments is None:
        attachments = []
    if not isinstance(attachments, list):
        raise ValueError("QQ attachments 必须为数组")
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("QQ 媒体信息格式无效")
        if item.get("url") is None:
            continue
        url = media_url(item.get("url"))
        if guild:
            segments.append(MessageSegment("image", {"file": url, "url": url, "name": ""}))
            continue
        if not isinstance(item.get("content_type"), str):
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
        segments.append(MessageSegment(kind, {"file": url, "url": url, "name": item.get("filename", "")}))
    if guild:
        segments.extend(MessageSegment("qq:embed", item) for item in data.get("embeds") or [])
        if data.get("ark"):
            segments.append(MessageSegment("qq:ark", data["ark"]))
    return Message(segments)


def _unescape(value: str) -> str:
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _ext_index(ext: list, name: str) -> str | None:
    """从 `message_scene.ext` 的 key=value 里取出消息索引。"""
    prefix = f"{name}="
    for value in ext:
        if isinstance(value, str) and value.startswith(prefix):
            index = value.removeprefix(prefix)
            if index:
                return index
    return None


def _quoted_element(elements: list, reference_index: str | None, current_index: str | None) -> dict | None:
    """挑出被引用的消息元素。

    QQ 引用消息的 `ref_msg_idx` 未必与 `msg_elements[].msg_idx` 相等（前者可能是
    `TMP_` 临时索引），甚至可能缺失；此时按官方约定退回 `msg_elements` 里唯一的
    非本条消息元素，避免整条引用被丢掉。
    """
    items = [element for element in elements if isinstance(element, dict)]
    if reference_index:
        for element in items:
            if element.get("msg_idx") is not None and str(element["msg_idx"]) == str(reference_index):
                return element
    candidates = [
        element
        for element in items
        if current_index is None
        or element.get("msg_idx") is None
        or str(element["msg_idx"]) != str(current_index)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates and len(items) == 1:
        return items[0]
    return None


def parse_event(payload: dict, *, bot_id: str, bot_user_id: str = "", bot_username: str | None = None) -> Event:
    if not isinstance(payload, dict) or payload.get("op") != 0 or type(payload.get("op")) is not int:
        raise ValueError("QQ 事件必须为 Dispatch 帧")
    event_type, data = payload.get("t"), payload.get("d")
    raw_data = deepcopy(data)
    if event_type in EVENT_CLASSES:
        model = EVENT_CLASSES[event_type].model_validate(
            {"event_id": payload.get("id"), **(data if isinstance(data, dict) else {})}
        )
        data = model.model_dump(exclude_none=True)
        payload = {**payload, "d": data}
    if event_type in NOTICE_TYPES:
        return _notice(payload, bot_id, raw_data)
    if event_type not in MESSAGE_TYPES:
        data = data if isinstance(data, dict) else {}
        message = data.get("message") or {}
        user = data.get("user_id") or data.get("author_id") or data.get("op_user_id") or ""
        channel = data.get("channel_id") or message.get("channel_id")
        guild = data.get("guild_id") or message.get("guild_id") or ""
        return Event(
            payload.get("id"),
            None,
            event_type.lower(),
            "qq",
            bot_id,
            user,
            "",
            SessionTarget("qq", bot_id, "channel" if channel else "guild", channel or guild),
            metadata={"qq_event_type": event_type, "qq_data": raw_data if isinstance(raw_data, dict) else {}},
        )
    if not isinstance(data, dict) or not isinstance(data.get("author"), dict):
        raise ValueError("QQ 消息缺少 author")
    author = data["author"]
    if event_type == "C2C_MESSAGE_CREATE":
        user = author.get("user_openid")
        target = SessionTarget("qq", bot_id, "private", user)
    elif event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
        user = author.get("member_openid")
        target = SessionTarget("qq", bot_id, "group", (data.get("group_openid")))
    else:
        user = author.get("id")
        channel, guild = (data.get("channel_id")), (data.get("guild_id"))
        target = (
            SessionTarget("qq", bot_id, "private", f"dms/{quote(guild, safe='')}/{quote(user, safe='')}")
            if event_type == "DIRECT_MESSAGE_CREATE"
            else SessionTarget("qq", bot_id, "channel", channel)
        )
    message_id = data.get("id")
    timestamp = data.get("timestamp")
    occurred_at = _occurred_at(timestamp)
    guild_message = target.kind == "channel" or event_type == "DIRECT_MESSAGE_CREATE"
    message = parse_message(data, guild=guild_message)
    if event_type.startswith("GROUP_") and message.segments and message.segments[0].type == "text":
        message.segments[0].data["text"] = message.segments[0].text_value().lstrip()
    mentions = data.get("mentions") or []
    if target.kind == "group":
        users = {entry["id"]: entry for entry in mentions if entry["scope"] == "single"}
        if not message.mentions():
            for mention in users.values():
                part = MessageSegment.mention(mention["id"], mention["username"])
                part.data["is_bot"] = mention["is_you"]
                if mention["is_you"]:
                    message.segments.insert(0, part)
                else:
                    message.segments.append(part)
        else:
            for part in message.segments:
                if part.type == "mention" and (mention := users.get(part.data["user_id"])):
                    part.data.update(name=mention["username"], is_bot=mention["is_you"])
    to_me = event_type in {
        "C2C_MESSAGE_CREATE",
        "GROUP_AT_MESSAGE_CREATE",
        "AT_MESSAGE_CREATE",
        "DIRECT_MESSAGE_CREATE",
    }
    if guild_message and bot_user_id:
        to_me = to_me or any(mention["id"] == bot_user_id for mention in mentions)
    role = author.get("member_role")
    if role is not None and role not in {"member", "admin", "owner"}:
        raise ValueError("QQ 群成员角色无效")
    metadata = {"qq_event_type": event_type, "qq_data": raw_data}
    if target.kind == "group" and role is not None:
        metadata["sender_role"] = role
    if event_type in {"AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}:
        metadata.update(guild_id=guild, channel_id=channel)
    reference = data.get("message_reference")
    reply = None
    quoted = None
    if reference is not None:
        if not isinstance(reference, dict):
            raise ValueError("QQ 引用信息无效")
        reply = ReplyTarget((reference.get("message_id")))
    if data.get("message_type") == 103:
        scene = data.get("message_scene") or {}
        elements = data.get("msg_elements") or []
        ext = scene.get("ext") or []
        if not isinstance(ext, list) or not isinstance(elements, list):
            raise ValueError("QQ 引用消息结构无效")
        reference_index = _ext_index(ext, "ref_msg_idx")
        current_index = _ext_index(ext, "msg_idx")
        if current_index is None and data.get("msg_idx") is not None:
            current_index = str(data["msg_idx"])
        quoted = _quoted_element(elements, reference_index, current_index)
        if quoted is not None:
            quoted_index = str(quoted["msg_idx"]) if quoted.get("msg_idx") is not None else reference_index
            metadata["qq_reply"] = deepcopy(quoted)
            reply_author = quoted.get("author") or {}
            author_key = "member_openid" if target.kind == "group" else "user_openid"
            reply = ReplyTarget(
                quoted_index or "",
                (reply_author[author_key]) if reply_author.get(author_key) else None,
                reply_author.get("username") or "",
            )
            if reply_author.get("bot") and reply_author.get("username") == bot_username:
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
                    known_mentions = {entry.get("id") for entry in mentions if entry.get("scope") == "single"}
                    if mentioned in (authors if authors else known_mentions):
                        if message.segments[index].data.get("is_bot"):
                            to_me = True
                        del message.segments[: index + 1]
                        if message.segments and message.segments[0].type == "text":
                            message.segments[0].data["text"] = message.segments[0].text_value().lstrip()
        elif reference_index is not None and reply is None:
            # QQ 可能只给引用索引，不回传 msg_elements；缓存仍能补出原文。
            reply = ReplyTarget(reference_index)
    # 临时诊断：只对带引用字段的消息打一行 INFO，确认 QQ 引用形态后可删除。
    scene_debug = data.get("message_scene")
    ext_debug = scene_debug.get("ext") if isinstance(scene_debug, dict) else None
    elements_debug = data.get("msg_elements")
    has_reference = bool(reference) or bool(elements_debug) or data.get("message_type") == 103
    has_reference = has_reference or bool(isinstance(ext_debug, list) and _ext_index(ext_debug, "ref_msg_idx"))
    if has_reference:
        logger.info(
            "[QQ] 引用字段 bot={} event={} type={} reference={} ext={} elements={} attachments={} resolved={} reply={}",
            bot_id,
            event_type,
            data.get("message_type"),
            bool(reference),
            ext_debug,
            len(elements_debug) if isinstance(elements_debug, list) else None,
            sum(len(element.get("attachments") or []) for element in elements_debug if isinstance(element, dict))
            if isinstance(elements_debug, list)
            else None,
            (quoted or {}).get("msg_idx") if quoted else None,
            reply.message_id if reply is not None else None,
        )
    if target.kind == "group" and event_type != "GROUP_AT_MESSAGE_CREATE":
        for part in message.segments:
            if part.type == "mention" and part.data.get("is_bot"):
                part.data["user_id"] = bot_user_id or bot_id
    original = deepcopy(message)
    if event_type == "GROUP_AT_MESSAGE_CREATE":
        original.segments.insert(0, MessageSegment.mention(bot_user_id or bot_id))
    else:

        def at_me(part):
            return part.type == "mention" and part.data["user_id"] == (bot_user_id or bot_id)

        if message.segments and at_me(message.segments[0]):
            to_me = True
            message.segments.pop(0)
            if message.segments and message.segments[0].type == "text":
                message.segments[0].data["text"] = message.segments[0].text_value().lstrip()
                if not message.segments[0].text_value():
                    message.segments.pop(0)
        elif message.segments:
            index = -1
            if (
                message.segments[-1].type == "text"
                and not message.segments[-1].text_value().strip()
                and len(message.segments) > 1
            ):
                index = -2
            if at_me(message.segments[index]):
                to_me = True
                del message.segments[index:]
    if not message.segments:
        message.segments.append(MessageSegment.text(""))
    event_id = payload.get("id")
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
