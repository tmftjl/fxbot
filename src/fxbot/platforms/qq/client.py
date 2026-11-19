"""QQ 官方消息客户端；只声明实际支持的能力，不模拟 OneBot 动作。"""

from __future__ import annotations

import html
import time
import asyncio
from copy import deepcopy
from typing import Literal
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import OrderedDict
from urllib.parse import unquote

from fxbot.core.errors import BotOffline, ActionFailed

from .api import QQHTTP
from .codec import api_path
from ...core import Event, Message, Capability, Capabilities, QuotedMessage, SessionTarget, UnsupportedCapability
from .models import Message as GuildMessage
from .recent import recent_reply, remember_sent, cached_message as cached_item_message
from .actions import QQ_API, QQActions
from .message import markdown
from ...core.context import current_event_context

_FILE_TYPES: dict[str, Literal[1, 2, 3, 4]] = {"image": 1, "video": 2, "audio": 3, "voice": 3, "file": 4}
_EVENT_REPLY_TYPES = {
    "INTERACTION_CREATE",
    "GROUP_ADD_ROBOT",
    "GROUP_MEMBER_ADD",
    "FRIEND_ADD",
}


def _member_rows(data: object) -> list[dict]:
    rows = data.get("members") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ActionFailed("QQ 成员响应必须包含 members 对象数组")
    return rows


def _member_record(data: object) -> dict:
    if not isinstance(data, dict):
        raise ActionFailed("QQ 成员响应必须为对象")
    user_id = data.get("member_openid")
    if not isinstance(user_id, str):
        raise ActionFailed("QQ 成员响应缺少 member_openid 字符串")
    name = data.get("username")
    role = data.get("member_role")
    if name is not None and not isinstance(name, str):
        raise ActionFailed("QQ 成员昵称必须为字符串")
    if role is not None and (not isinstance(role, str) or role not in {"member", "owner", "admin"}):
        raise ActionFailed("QQ 成员角色无效")
    return {**data, "user_id": user_id, "nickname": name if name is not None else "", "role": role}


class QQClient:
    platform = "qq"

    def __init__(self, app_id: str, api: QQHTTP, *, available=None, sequences=None) -> None:
        self.bot_id = app_id
        self.api = api
        self.actions = QQActions(self._request, self._put_upload)
        self.message_transform = lambda target, message: message
        self.capabilities = (
            Capabilities()
            .provide(Capability.REPLY, self)
            .provide(Capability.MEDIA, self)
            .provide(Capability.VIDEO, self)
            .provide(Capability.FILE, self)
            .provide(Capability.RECALL, self)
            .provide(Capability.MEMBER_LOOKUP, self)
            .provide(Capability.GROUP_ROLE, self)
            .provide(Capability.TARGET_LOOKUP, self)
            .provide(Capability.AVATAR, self)
            .provide(Capability.MEMBER_MODERATION, self)
            .provide(Capability.MUTED_MEMBERS, self)
            .provide(QQ_API, self.actions)
        )
        self._sequence = sequences if sequences is not None else OrderedDict()
        self._sent: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._moderation_lock = asyncio.Lock()
        self._available = available

    def _online(self):
        if self._available is not None and not self._available():
            raise BotOffline("QQ 网关连接已失效")

    def avatar_url(self, user_id: str) -> str:
        """QQ 官方头像按 AppID + OpenID 拼接。"""
        return f"https://q.qlogo.cn/qqapp/{self.bot_id}/{(user_id)}/640"

    async def _request(self, method, path, **kwargs):
        self._online()
        if kwargs.get("files") is not None or path.endswith("/files"):
            kwargs["upload"] = True
        return await self.api.request(method, path, available=self._online, **kwargs)

    async def _put_upload(self, url, content):
        self._online()
        await self.api.put_upload_part(url, content, available=self._online)

    def _target(self, target: SessionTarget) -> tuple[str, str]:
        if (target.platform, target.bot_id) != (self.platform, self.bot_id):
            raise ValueError("QQ 发送目标不属于当前 Bot")
        if target.kind == "private" and target.target_id.startswith("dms/"):
            parts = target.target_id.split("/")
            if len(parts) != 3:
                raise ValueError("QQ 频道私信目标格式无效")
            return "dms", unquote(parts[1])
        routes = {"private": "v2/users", "group": "v2/groups", "channel": "channels"}
        if target.kind not in routes:
            raise UnsupportedCapability("QQ 不支持此会话目标")
        return routes[target.kind], (target.target_id)

    def _current_event(self, target: SessionTarget) -> Event | None:
        try:
            runtime = current_event_context()
        except RuntimeError:
            return None
        if runtime.bot is not self or runtime.event.target != target:
            return None
        return runtime.event

    async def send(self, target: SessionTarget, message: Message) -> dict:
        self._online()
        event = self._current_event(target)
        if (
            event is not None
            and event.message_id is None
            and event.metadata.get("qq_event_type") not in _EVENT_REPLY_TYPES
        ):
            raise RuntimeError("Event cannot be replied to!")
        if event is not None and event.metadata.get("qq_event_type") == "INTERACTION_CREATE":
            data = event.metadata["qq_data"]
            routes = (
                ("group_openid", "v2/groups"),
                ("channel_id", "channels"),
                ("user_openid", "v2/users"),
                ("guild_id", "dms"),
            )
            selected = next(((route, data[key]) for key, route in routes if data.get(key)), None)
            if selected is None:
                raise RuntimeError("Event cannot be replied to!")
            route, target_id = selected
        else:
            route, target_id = self._target(target)
        message = self.message_transform(target, message)
        if route == "v2/groups" and event is not None and self._is_member_add_mention(event, message):
            # QQ 群文本消息不支持 @；与原版一致，入群欢迎改用 markdown 的 at-user 语法。
            return await self._send_member_add_mention(target, event, message)
        content, advanced = [], {}
        media = {}
        reference = passive = None
        for part in message.segments:
            if part.type == "text":
                text = part.text_value()
                content.append(html.escape(text, quote=False) if route in {"channels", "dms"} else text)
            elif part.type == "mention":
                content.append(f"<@{part.data['user_id']}>")
            elif part.type == "mention_all":
                content.append("@everyone")
            elif part.type == "qq:mention_channel":
                content.append(f"<#{part.data['channel_id']}>")
            elif part.type == "qq:emoji":
                content.append(f"<emoji:{part.data['id']}>")
            elif part.type == "reply":
                reference = part.data["message_id"]
            elif part.type == "qq:passive":
                passive = part.data["msg_id"]
            elif part.type in _FILE_TYPES:
                source = part.data.get("file", part.data.get("url"))
                kind = "audio" if part.type == "voice" else part.type
                media[(kind, isinstance(source, bytes))] = (source, part.data.get("name") or "default")
            elif part.type.startswith("qq:"):
                name = part.type.removeprefix("qq:")
                if name in {
                    "embed",
                    "ark",
                    "markdown",
                    "keyboard",
                    "stream",
                    "prompt_keyboard",
                    "action_button",
                    "media",
                }:
                    advanced[name] = dict(part.data)
        payload = {"content": "".join(content) or None, **advanced}
        if event is not None and event.message_id is not None:
            payload["msg_id"] = event.message_id
            if route == "v2/users":
                target_id = event.metadata["qq_data"]["author"]["id"]
            if route.startswith("v2/"):
                event.metadata["qq_reply_seq"] = event.metadata.get("qq_reply_seq", 0) + 1
                payload["msg_seq"] = event.metadata["qq_reply_seq"]
        elif event is not None and event.metadata.get("qq_event_type") in _EVENT_REPLY_TYPES:
            payload["event_id"] = event.event_id
        if passive is not None:
            payload["msg_id"] = passive
            if route.startswith("v2/"):
                payload["event_id"] = passive
                key = (target.key(), passive)
                now = time.monotonic()
                while self._sequence and next(iter(self._sequence.values()))[1] <= now:
                    self._sequence.popitem(last=False)
                seq = self._sequence.get(key, (0, 0))[0] + 1
                self._sequence[key] = (seq, now + 300)
                self._sequence.move_to_end(key)
                if len(self._sequence) > 4096:
                    self._sequence.popitem(last=False)
                payload["msg_seq"] = seq
        if reference is not None:
            if event is not None and route.startswith("v2/") and not reference.startswith("REFIDX"):
                ext = (event.metadata.get("qq_data", {}).get("message_scene") or {}).get("ext", [])
                reference = next((v.partition("=")[-1] for v in ext if v.startswith("msg_idx=")), reference)
            payload["message_reference"] = {"message_id": reference}
        if route in {"channels", "dms"}:
            if ("image", False) in media:
                payload["image"] = media[("image", False)][0]
            if ("image", True) in media:
                payload["file_image"] = media[("image", True)][0]
            action = self.actions.post_messages if route == "channels" else self.actions.post_dms_messages
            identity = {"channel_id" if route == "channels" else "guild_id": target_id}
            result = await action(**identity, **payload)
        else:
            msg_type = (
                4
                if "embed" in advanced
                else 3
                if "ark" in advanced
                else 2
                if advanced.keys() & {"markdown", "keyboard", "prompt_keyboard", "action_button"}
                else 7
                if media or "media" in advanced
                else 0
            )
            payload["msg_type"] = msg_type
            if msg_type == 7 and media:
                key = next(
                    (kind, local)
                    for local in (False, True)
                    for kind in ("image", "video", "audio", "file")
                    if (kind, local) in media
                )
                source, file_name = media[key]
                file_type = _FILE_TYPES[key[0]]
                if isinstance(source, bytes) and len(source) > 10 * 1024 * 1024:
                    action = self.actions.post_group_upload if route == "v2/groups" else self.actions.post_c2c_upload
                    uploaded = await action(target_id, file_type, file_name, source, False)
                else:
                    action = self.actions.post_group_files if route == "v2/groups" else self.actions.post_c2c_files
                    identity = {"group_openid" if route == "v2/groups" else "openid": target_id}
                    uploaded = await action(
                        **identity,
                        file_type=file_type,
                        srv_send_msg=False,
                        **{"file_data" if isinstance(source, bytes) else "url": source},
                    )
                payload["media"] = {"file_info": uploaded["file_info"]} if uploaded.get("file_info") else None
            action = self.actions.post_group_messages if route == "v2/groups" else self.actions.post_c2c_messages
            identity = {"group_openid" if route == "v2/groups" else "openid": target_id}
            result = await action(**identity, **payload)
        message_id = result.get("id")
        if message_id is not None:
            self._sent[message_id] = (route, target_id)
            if len(self._sent) > 4096:
                self._sent.popitem(last=False)
        ext_info = result.get("ext_info")
        if route == "v2/groups" and isinstance(ext_info, dict) and message_id:
            remember_sent(self, target_id, message, message_id, ext_info.get("ref_idx"))
        return {"message_id": message_id, **({"ext_info": ext_info} if ext_info is not None else {})}

    @staticmethod
    def _is_member_add_mention(event: Event, message: Message) -> bool:
        """首段 @ 的正好是新成员时，才按入群欢迎处理。"""
        if event.metadata.get("qq_event_type") != "GROUP_MEMBER_ADD":
            return False
        if not message.segments or message.segments[0].type != "mention":
            return False
        return str(message.segments[0].data.get("user_id") or "") == str(event.user_id)

    async def _send_member_add_mention(self, target: SessionTarget, event: Event, message: Message) -> dict:
        """QQ 群文本不支持 @：提及与文字走 markdown，图片按原版逐条发送。"""
        mention = f'<qqbot-at-user id="{event.user_id}" />'
        text = "".join(part.text_value() for part in message.segments[1:] if part.type == "text").strip()
        result = await self.send(target, Message([markdown(f"{mention} {text or '欢迎欢迎'}")]))
        for part in message.segments[1:]:
            if part.type == "image":
                result = await self.send(target, Message([part]))
        return result

    async def resolve_media(self, segment) -> str | bytes:
        if segment.type not in {"image", "voice", "audio", "video", "file"}:
            raise UnsupportedCapability("不是 QQ 媒体段")
        source = segment.data.get("file")
        return source

    async def get_reply(self, event: Event) -> QuotedMessage | None:
        if (event.platform, event.bot_id) != (self.platform, self.bot_id):
            raise ValueError("引用事件不属于当前 QQ Bot")
        if event.reply is None:
            return None
        raw = event.metadata.get("qq_reply")
        if event.target.kind == "channel" or event.target.target_id.startswith("dms/"):
            from .codec import parse_message

            channel_id = event.metadata.get("channel_id")
            if not isinstance(channel_id, str):
                raise ValueError("QQ 引用缺少频道身份")
            data = (
                raw
                if isinstance(raw, dict)
                else await self.actions.get_message_of_id(channel_id=channel_id, message_id=event.reply.message_id)
            )
            if isinstance(data, dict) and isinstance(data.get("message"), dict):
                data = data["message"]
            data = GuildMessage.model_validate(data).model_dump(exclude_none=True)
            event.metadata["qq_reply"] = deepcopy(data)
            author = data["author"]
            sender = author["id"]
            name = author.get("username") if isinstance(author.get("username"), str) else ""
            return QuotedMessage(event.reply.message_id, sender, name, parse_message(data, guild=True))
        from .codec import parse_message

        message = parse_message(raw) if isinstance(raw, dict) else Message([])
        author = raw.get("author") if isinstance(raw, dict) and isinstance(raw.get("author"), dict) else {}
        key = "member_openid" if event.is_group else "user_openid"
        sender = (author[key]) if author.get(key) else None
        name = author.get("username") if isinstance(author.get("username"), str) else ""
        cached = recent_reply(self, event)
        if cached is not None:
            cached_message = cached_item_message(cached)
            if not any(part.type == "text" and part.text_value().strip() for part in message.segments):
                message.segments[:0] = [part for part in cached_message.segments if part.type == "text"]
            if not any(part.type == "image" for part in message.segments):
                message.segments.extend(part for part in cached_message.segments if part.type == "image")
            sender = sender or cached.get("sender_id") or None
            name = name or str(cached.get("sender_name") or "")
        return QuotedMessage(event.reply.message_id, sender, name, message)

    async def delete_message(self, message_id: str):
        location = self._sent.get(message_id)
        if location is None:
            raise UnsupportedCapability("撤回需要本客户端已发送消息的会话记录；指定会话的撤回请使用 QQ_API")
        route, target_id = location
        return await self._request("DELETE", api_path(*route.split("/"), target_id, "messages", message_id))

    async def send_file(self, target: SessionTarget, path: Path, name: str):
        self._online()
        route, _ = self._target(target)
        if not route.startswith("v2/"):
            raise UnsupportedCapability("QQ 文件发送只接入普通群和 C2C")
        data = await asyncio.to_thread(path.read_bytes)
        from ...core import MessageSegment

        return await self.send(target, Message([MessageSegment("file", {"file": data, "name": name})]))

    async def get_member(self, group_id: str, user_id: str) -> dict:
        member = _member_record(await self.actions.get_group_member(group_openid=group_id, member_openid=user_id))
        if member["user_id"] != user_id:
            raise ActionFailed("QQ 成员查询返回了其他成员")
        return member

    async def get_members(self, group_id: str) -> list[dict]:
        members: list[dict] = []
        cursor = None
        seen = set()
        for _ in range(1000):
            data = await self.actions.get_group_members(group_openid=(group_id), cursor=cursor)
            members.extend(_member_record(member) for member in _member_rows(data))
            cursor = data.get("next_cursor")
            if cursor is None or cursor == "":
                return members
            if not isinstance(cursor, str):
                raise ActionFailed("QQ 群成员分页游标必须为字符串")
            if cursor in seen:
                raise ActionFailed("QQ 群成员分页游标未前进")
            seen.add(cursor)
        raise ActionFailed("QQ 群成员查询超过分页预算")

    async def member_name(self, group_id: str, user_id: str) -> str:
        return (await self.get_member(group_id, user_id))["nickname"]

    async def member_role(self, group_id: str, user_id: str) -> str:
        if user_id == self.bot_id:
            return await self.bot_group_role(group_id)
        role = (await self.get_member(group_id, user_id))["role"]
        if not isinstance(role, str) or role not in {"member", "owner", "admin"}:
            raise ActionFailed("QQ 群成员角色缺失或无效")
        return role

    async def bot_group_role(self, group_id: str) -> str:
        data = await self.actions.get_group_bot_state(group_id=(group_id))
        role = data.get("member_role")
        if not isinstance(role, str) or role not in {"member", "owner", "admin"}:
            raise ActionFailed("QQ Bot 群角色缺失或无效")
        return role

    async def target_name(self, target: SessionTarget) -> str:
        self._target(target)
        if target.kind != "group":
            raise UnsupportedCapability("QQ 只提供群名称查询")
        return (await self.actions.get_group_info(group_id=target.target_id))["group_name"]

    async def ban(self, group_id: str, user_id: str, duration: int):
        if type(duration) is not int or not 0 <= duration <= 2592000:
            raise ValueError("QQ 禁言时长必须为 0 到 30 天")
        async with self._moderation_lock:
            operation = "del"
            if duration:
                current = await self.actions.get_group_mute_setting(group_id=group_id)
                members = [_member_record(member) for member in _member_rows(current)]
                operation = "update" if any(member["user_id"] == user_id for member in members) else "add"
            member = {
                "op": operation,
                "member_openid": user_id,
                "mute_expire_at": (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat()
                if duration
                else "",
            }
            return await self.actions.set_group_members_mute(group_id=group_id, members=[member])

    async def kick(self, group_id: str, user_id: str, *, reject_add_request: bool = False):
        result = await self.actions.batch_remove_group_members(
            group_openid=(group_id),
            member_openids=[(user_id)],
            add_to_member_blacklist=reject_add_request,
        )
        if result.get("add_to_member_blacklist_fail_openids"):
            raise ValueError("QQ 已处理移除，但加入黑名单未全部成功，未重试")
        return result

    async def get_muted_members(self, group_id: str) -> list[dict]:
        data = await self.actions.get_group_mute_setting(group_id=(group_id))
        now = datetime.now(timezone.utc)
        members = []
        for raw in _member_rows(data):
            member = _member_record(raw)
            timestamp = raw.get("mute_expire_at")
            if not isinstance(timestamp, str) or not isinstance(raw.get("username"), str):
                raise ActionFailed("QQ 禁言成员缺少有效的到期时间或昵称")
            try:
                expiry = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as error:
                raise ActionFailed("QQ 禁言到期时间无效") from error
            if expiry.tzinfo is None:
                raise ActionFailed("QQ 禁言到期时间必须带时区")
            if expiry > now:
                members.append(
                    {
                        **member,
                        "remaining": int((expiry - now).total_seconds()),
                        "mute_until": int(expiry.timestamp()),
                    }
                )
        return members
