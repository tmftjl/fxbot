"""QQ 官方消息客户端；只声明实际支持的能力，不模拟 OneBot 动作。"""

from __future__ import annotations

import html
import time
import base64
import asyncio
from typing import Any, Literal
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import OrderedDict
from urllib.parse import urlsplit

from fxbot.core.errors import BotOffline, ActionFailed

from .api import QQHTTP
from .codec import api_path, opaque_id, identifier
from ...core import Event, Message, Capability, Capabilities, QuotedMessage, SessionTarget, UnsupportedCapability
from .actions import QQ_API, QQActions
from ...core.context import current_event_context

_FILE_TYPES: dict[str, Literal[1, 2, 3, 4]] = {"image": 1, "video": 2, "audio": 3, "voice": 3, "file": 4}
_EVENT_REPLY_TYPES = {
    "INTERACTION_CREATE",
    "GROUP_ADD_ROBOT",
    "GROUP_MEMBER_ADD",
    "GROUP_MSG_RECEIVE",
    "FRIEND_ADD",
    "C2C_MSG_RECEIVE",
}


def _member_rows(data: object) -> list[dict]:
    rows = data.get("members") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ActionFailed("QQ 成员响应必须包含 members 对象数组")
    return rows


def _member_record(data: object) -> dict:
    if not isinstance(data, dict):
        raise ActionFailed("QQ 成员响应必须为对象")
    try:
        user_id = identifier(data.get("member_openid"))
    except ValueError as error:
        raise ActionFailed("QQ 成员响应缺少有效 member_openid") from error
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
            .provide(Capability.MEMBER_MODERATION, self)
            .provide(Capability.MUTED_MEMBERS, self)
            .provide(QQ_API, self.actions)
        )
        self._sequence = sequences if sequences is not None else OrderedDict()
        self._sent: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._send_lock = asyncio.Lock()
        self._moderation_lock = asyncio.Lock()
        self._available = available

    def _online(self):
        if self._available is not None and not self._available():
            raise BotOffline("QQ 网关连接已失效")

    async def _request(self, method, path, **kwargs):
        self._online()
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
            identifier(parts[2])
            return "dms", identifier(parts[1])
        routes = {"private": "v2/users", "group": "v2/groups", "channel": "channels"}
        if target.kind not in routes:
            raise UnsupportedCapability("QQ 不支持此会话目标")
        return routes[target.kind], identifier(target.target_id)

    def _current_event(self, target: SessionTarget) -> Event | None:
        try:
            runtime = current_event_context()
        except RuntimeError:
            return None
        if runtime.bot is not self or runtime.event.target != target:
            return None
        return runtime.event

    @staticmethod
    def _url(source) -> str:
        if not isinstance(source, str):
            raise ValueError("媒体来源必须为字节或 HTTP(S) 地址")
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("QQ 出站远程媒体必须是 HTTP(S) 地址")
        return source

    async def send(self, target: SessionTarget, message: Message) -> dict:
        self._online()
        route, target_id = self._target(target)
        message = self.message_transform(target, message)
        content = []
        media = []
        advanced = {}
        reference = None
        for part in message.segments:
            if part.type == "text":
                value = part.text_value()
                content.append(html.escape(value, quote=False) if route in {"channels", "dms"} else value)
            elif part.type in {"mention", "mention_all"}:
                if route not in {"channels", "v2/groups"}:
                    raise UnsupportedCapability("当前 QQ 会话不支持该提及段")
                content.append(
                    "@everyone" if part.type == "mention_all" else f"<@{identifier(part.data.get('user_id'))}>"
                )
            elif part.type == "reply":
                if reference is not None:
                    raise ValueError("一条消息只能包含一个引用")
                reference = opaque_id(part.data.get("message_id"))
            elif part.type in {"image", "voice", "audio", "video", "file"}:
                source = part.data.get("file")
                if not isinstance(source, bytes):
                    self._url(source)
                if isinstance(source, bytes) and len(source) > 200 * 1024 * 1024:
                    raise ValueError("QQ 媒体不能超过 200 MiB")
                media.append((part.type, source, part.data.get("name") or "upload.bin"))
            elif part.type in {
                "qq:markdown",
                "qq:keyboard",
                "qq:ark",
                "qq:embed",
                "qq:stream",
                "qq:prompt_keyboard",
                "qq:action_button",
                "qq:media",
            }:
                name = part.type.removeprefix("qq:")
                if name in advanced:
                    raise ValueError(f"QQ 同一消息不能重复声明 {name}")
                if route not in {"v2/users", "v2/groups"}:
                    raise UnsupportedCapability("QQ 高级消息仅接入普通群和 C2C")
                if route == "v2/groups" and name in {"stream", "prompt_keyboard", "action_button"}:
                    raise UnsupportedCapability("参考协议仅在 C2C 声明此消息字段")
                advanced[name] = dict(part.data)
            elif part.type == "qq:emoji":
                content.append(f"<emoji:{identifier(part.data['id'])}>")
            else:
                raise UnsupportedCapability(f"QQ 未实现此出站消息段: {part.type}")
        if len(media) > 1 or media and "media" in advanced:
            raise UnsupportedCapability("QQ 单条发送仅支持一项媒体，不拆分重试")
        if (media or "media" in advanced) and advanced.keys() - {"media"}:
            raise UnsupportedCapability("QQ 不支持高级消息格式与媒体混合发送")
        if route in {"channels", "dms"} and media and media[0][0] != "image":
            raise UnsupportedCapability("QQ 频道/频道私信当前仅支持图片媒体")
        text = "".join(content)
        if not text and not media and not advanced:
            raise ValueError("QQ 不能发送空消息")
        async with self._send_lock:
            self._online()
            payload: dict[str, Any] = {"content": text, **advanced}
            event = self._current_event(target)
            reply_id = event.message_id if event is not None else None
            if (
                reply_id is None
                and event is not None
                and event.event_id
                and event.metadata.get("qq_event_type") in _EVENT_REPLY_TYPES
            ):
                payload["event_id"] = event.event_id
            if reply_id is not None:
                payload["msg_id"] = reply_id
                if route.startswith("v2/"):
                    key = (target.key(), reply_id)
                    now = time.monotonic()
                    while self._sequence and next(iter(self._sequence.values()))[1] <= now:
                        self._sequence.popitem(last=False)
                    sequence = self._sequence.get(key, (0, now))[0] + 1
                    self._sequence[key] = (sequence, now + 300)
                    self._sequence.move_to_end(key)
                    if len(self._sequence) > 4096:
                        self._sequence.popitem(last=False)
                    payload["msg_seq"] = sequence
            if reference is not None:
                if event is not None and reference == event.message_id:
                    ext = event.metadata.get("qq_data", {}).get("message_scene", {}).get("ext", [])
                    reference = next(
                        (value.removeprefix("msg_idx=") for value in ext if value.startswith("msg_idx=")), reference
                    )
                payload["message_reference"] = {"message_id": reference}
            path = api_path(*route.split("/"), target_id, "messages")
            files = None
            if route.startswith("v2/"):
                payload["msg_type"] = (
                    4
                    if "embed" in advanced
                    else 3
                    if "ark" in advanced
                    else 2
                    if any(name in advanced for name in ("markdown", "keyboard", "prompt_keyboard", "action_button"))
                    else 7
                    if media or "media" in advanced
                    else 0
                )
                payload["timestamp"] = int(time.time())
                if media:
                    kind, source, file_name = media[0]
                    file_type = _FILE_TYPES[kind]
                    upload: dict[str, Any] = {
                        "file_type": file_type,
                        "srv_send_msg": False,
                    }
                    if isinstance(source, bytes) and len(source) > 10 * 1024 * 1024:
                        if route == "v2/groups":
                            uploaded = await self.actions.post_group_upload(
                                target_id, file_type, file_name, source, False
                            )
                        else:
                            uploaded = await self.actions.post_c2c_upload(
                                target_id, file_type, file_name, source, False
                            )
                    else:
                        upload["file_data" if isinstance(source, bytes) else "url"] = (
                            base64.b64encode(source).decode("ascii") if isinstance(source, bytes) else source
                        )
                        uploaded = await self._request(
                            "POST", api_path(*route.split("/"), target_id, "files"), json=upload
                        )
                    if not isinstance(uploaded.get("file_info"), str) or not uploaded["file_info"]:
                        raise ValueError("QQ 媒体上传未返回 file_info")
                    payload["media"] = {"file_info": uploaded["file_info"]}
            elif media:
                source = media[0][1]
                if isinstance(source, bytes):
                    files = {"file_image": ("image.png", source, "application/octet-stream")}
                else:
                    payload["image"] = source
            if files is None:
                result = await self._request("POST", path, json=payload)
            else:
                import json

                fields = {
                    key: json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
                    for key, value in payload.items()
                }
                result = await self._request("POST", path, data=fields, files=files)
            message_id = result.get("id")
            if not isinstance(message_id, str) or not message_id:
                raise ValueError("QQ 发送成功但缺少消息 ID，未重试")
            self._sent[message_id] = (route, target_id)
            if len(self._sent) > 4096:
                self._sent.popitem(last=False)
            return {"message_id": message_id, **({"ext_info": result["ext_info"]} if "ext_info" in result else {})}

    async def resolve_media(self, segment) -> str | bytes:
        if segment.type not in {"image", "voice", "audio", "video", "file"}:
            raise UnsupportedCapability("不是 QQ 媒体段")
        source = segment.data.get("file")
        return source if isinstance(source, bytes) else self._url(source)

    async def get_reply(self, event: Event) -> QuotedMessage | None:
        if (event.platform, event.bot_id) != (self.platform, self.bot_id):
            raise ValueError("引用事件不属于当前 QQ Bot")
        if event.reply is None:
            return None
        raw = event.metadata.get("qq_reply")
        if raw is not None:
            from .codec import parse_message

            author = raw.get("author") or {}
            key = "member_openid" if event.is_group else "user_openid"
            sender = identifier(author[key]) if author.get(key) else None
            return QuotedMessage(event.reply.message_id, sender, author.get("username") or "", parse_message(raw))
        return QuotedMessage(event.reply.message_id, event.reply.user_id, event.reply.user_name)

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
        if not 0 < path.stat().st_size <= 200 * 1024 * 1024:
            raise ValueError("QQ 文件大小必须为 1 字节到 200 MiB")
        data = await asyncio.to_thread(path.read_bytes)
        from ...core import MessageSegment

        return await self.send(target, Message([MessageSegment("file", {"file": data, "name": name})]))

    async def get_member(self, group_id: str, user_id: str) -> dict:
        group_id, user_id = identifier(group_id), identifier(user_id)
        member = _member_record(await self.actions.get_group_member(group_openid=group_id, member_openid=user_id))
        if member["user_id"] != user_id:
            raise ActionFailed("QQ 成员查询返回了其他成员")
        return member

    async def get_members(self, group_id: str) -> list[dict]:
        members: list[dict] = []
        cursor = None
        seen = set()
        for _ in range(1000):
            data = await self.actions.get_group_members(group_openid=identifier(group_id), cursor=cursor)
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
        data = await self.actions.get_group_bot_state(group_id=identifier(group_id))
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
        group_id, user_id = identifier(group_id), identifier(user_id)
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
            group_openid=identifier(group_id),
            member_openids=[identifier(user_id)],
            add_to_member_blacklist=reject_add_request,
        )
        if result.get("add_to_member_blacklist_fail_openids"):
            raise ValueError("QQ 已处理移除，但加入黑名单未全部成功，未重试")
        return result

    async def get_muted_members(self, group_id: str) -> list[dict]:
        data = await self.actions.get_group_mute_setting(group_id=identifier(group_id))
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
