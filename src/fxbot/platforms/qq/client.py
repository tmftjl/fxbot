"""QQ 官方消息客户端；只声明实际支持的能力，不模拟 OneBot 动作。"""

from __future__ import annotations

import html
import time
import base64
import asyncio
from pathlib import Path
from collections import OrderedDict
from urllib.parse import quote, urlsplit

from .api import QQHTTP
from .codec import opaque_id, identifier
from ...core import Event, Message, Capability, QuotedMessage, SessionTarget, UnsupportedCapability
from ...core.context import current_runtime
from ...runtime.actions import BotOffline


class QQClient:
    platform = "qq"
    capabilities = frozenset({Capability.REPLY, Capability.VIDEO})

    def __init__(self, app_id: str, api: QQHTTP, *, available=None, sequences=None) -> None:
        self.bot_id = app_id
        self.api = api
        self.message_transform = lambda target, message: message
        self._sequence = sequences if sequences is not None else OrderedDict()
        self._sent = OrderedDict()
        self._send_lock = asyncio.Lock()
        self._available = available

    def _online(self):
        if self._available is not None and not self._available():
            raise BotOffline("QQ 网关连接已失效")

    async def _request(self, method, path, **kwargs):
        self._online()
        return await self.api.request(method, path, available=self._online, **kwargs)

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

    def _reference(self, target: SessionTarget) -> str | None:
        try:
            runtime = current_runtime()
        except RuntimeError:
            return None
        if runtime.bot is not self or runtime.event.target != target:
            return None
        return runtime.event.message_id

    @staticmethod
    def _url(source) -> str:
        if not isinstance(source, str):
            raise ValueError("媒体来源必须为字节或 HTTPS 地址")
        parsed = urlsplit(source)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("QQ 出站远程媒体必须是 HTTPS")
        return source

    async def send(self, target: SessionTarget, message: Message) -> dict:
        self._online()
        route, target_id = self._target(target)
        message = self.message_transform(target, message)
        content = []
        media = []
        reference = None
        for part in message.segments:
            if part.type == "text":
                value = part.text_value()
                content.append(html.escape(value, quote=False) if route in {"channels", "dms"} else value)
            elif part.type in {"mention", "mention_all"}:
                if route not in {"channels", "v2/groups"} or (route == "v2/groups" and part.type == "mention_all"):
                    raise UnsupportedCapability("当前 QQ 会话不支持该提及段")
                content.append(
                    "@everyone" if part.type == "mention_all" else f"<@{identifier(part.data.get('user_id'))}>"
                )
            elif part.type == "reply":
                if reference is not None:
                    raise ValueError("一条消息只能包含一个引用")
                reference = opaque_id(part.data.get("message_id"))
            elif part.type in {"image", "voice", "audio", "video"}:
                source = part.data.get("file")
                if not isinstance(source, bytes):
                    self._url(source)
                if isinstance(source, bytes) and len(source) > 20 * 1024 * 1024:
                    raise ValueError("QQ 内联媒体不能超过 20 MiB")
                media.append((part.type, source))
            else:
                raise UnsupportedCapability(f"QQ 未实现此出站消息段: {part.type}")
        if len(media) > 1:
            raise UnsupportedCapability("QQ 单条发送仅支持一项媒体，不拆分重试")
        if route in {"channels", "dms"} and media and media[0][0] != "image":
            raise UnsupportedCapability("QQ 频道/频道私信当前仅支持图片媒体")
        text = "".join(content)
        if not text and not media:
            raise ValueError("QQ 不能发送空消息")
        async with self._send_lock:
            self._online()
            payload = {"content": text}
            reply_id = self._reference(target)
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
                payload["message_reference"] = {"message_id": reference}
            path = f"/{route}/{quote(target_id, safe='')}/messages"
            files = None
            if route.startswith("v2/"):
                payload["msg_type"] = 7 if media else 0
                if media:
                    kind, source = media[0]
                    upload = {
                        "file_type": {"image": 1, "video": 2, "audio": 3, "voice": 3}[kind],
                        "srv_send_msg": False,
                    }
                    upload["file_data" if isinstance(source, bytes) else "url"] = (
                        base64.b64encode(source).decode("ascii") if isinstance(source, bytes) else source
                    )
                    uploaded = await self._request("POST", f"/{route}/{quote(target_id, safe='')}/files", json=upload)
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
            return {"message_id": message_id}

    async def call(self, action: str, **params):
        raise UnsupportedCapability("QQ 客户端不提供 OneBot 或任意 HTTP 动作映射")

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
        return QuotedMessage(event.reply.message_id, event.reply.user_id, event.reply.user_name)

    async def delete_message(self, message_id: str):
        location = self._sent.get(message_id)
        if location is None or location[0] not in {"channels", "dms"}:
            raise UnsupportedCapability("只支持撤回本连接已发送的频道/频道私信消息")
        route, target_id = location
        return await self._request(
            "DELETE", f"/{route}/{quote(target_id, safe='')}/messages/{quote(message_id, safe='')}"
        )

    async def get_groups(self):
        raise UnsupportedCapability("QQ 官方不提供普通群列表")

    async def get_user(self, user_id: str):
        raise UnsupportedCapability("QQ 官方不提供按 C2C OpenID 查询用户资料")

    async def get_member(self, group_id: str, user_id: str):
        raise UnsupportedCapability("QQ 官方不提供普通群成员查询")

    async def member_role(self, group_id: str, user_id: str):
        raise UnsupportedCapability("QQ 官方不提供普通群成员角色查询")

    async def bot_group_role(self, group_id: str):
        raise UnsupportedCapability("QQ 官方不提供普通群 Bot 角色查询")

    async def member_name(self, group_id: str, user_id: str):
        raise UnsupportedCapability("QQ 官方不提供普通群成员名称查询")

    async def target_name(self, target: SessionTarget):
        self._target(target)
        raise UnsupportedCapability("QQ 官方不提供普通会话名称查询")

    def avatar_url(self, user_id: str):
        raise UnsupportedCapability("QQ OpenID 不是 QQ 号，不能构造 QQ 头像 URL")

    async def ban(self, group_id: str, user_id: str, duration: int):
        raise UnsupportedCapability("QQ 普通群不支持此禁言操作")

    async def kick(self, group_id: str, user_id: str, *, reject_add_request: bool = False):
        raise UnsupportedCapability("QQ 普通群不支持此移出成员操作")

    async def set_admin(self, group_id: str, user_id: str, *, enabled: bool):
        raise UnsupportedCapability("QQ 普通群不支持设置管理员")

    async def set_whole_ban(self, group_id: str, *, enabled: bool):
        raise UnsupportedCapability("QQ 普通群不支持全群禁言")

    async def set_special_title(self, group_id: str, user_id: str, title: str):
        raise UnsupportedCapability("QQ 普通群不支持群头衔")

    async def set_essence(self, message_id: str, *, enabled: bool):
        raise UnsupportedCapability("QQ 不支持精华消息操作")

    async def get_muted_members(self, group_id: str):
        raise UnsupportedCapability("QQ 不支持禁言列表查询")

    async def like(self, user_id: str, times: int):
        raise UnsupportedCapability("QQ 官方不支持好友点赞")

    async def leave_group(self, group_id: str):
        raise UnsupportedCapability("QQ 官方不支持此退群操作")

    async def send_forward(self, target: SessionTarget, messages: list[Message]):
        raise UnsupportedCapability("QQ 官方不支持合并转发")

    async def get_forward_messages(self, forward_id: str):
        raise UnsupportedCapability("QQ 官方不支持合并转发查询")

    async def get_forward_ids(self, message_id: str):
        raise UnsupportedCapability("QQ 官方不支持合并转发查询")

    async def fetch_group_history(self, target: SessionTarget, *, start_time: int, end_time: int, limit: int):
        raise UnsupportedCapability("QQ 官方不支持普通群历史消息查询")

    async def send_file(self, target: SessionTarget, path: Path, name: str):
        raise UnsupportedCapability("QQ 文件分块上传尚未实现")
