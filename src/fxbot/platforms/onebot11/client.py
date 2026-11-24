"""OneBot V11 的直接动作客户端。"""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path
from collections.abc import Callable, Awaitable

from .codec import _id, _copy, parse_message, encode_message
from ...core import (
    Event,
    Message,
    Capability,
    Capabilities,
    QuotedMessage,
    SessionTarget,
    MessageSegment,
    UnsupportedCapability,
)

ActionSender = Callable[..., Awaitable[Any]]
FileUploader = Callable[[SessionTarget, Path, str], Awaitable[Any]]
# 合并转发要等 NapCat 把视频/图片上传到 QQ，明显慢于普通动作。
FORWARD_TIMEOUT = 120.0


class OneBot11Client:
    """将核心发送请求交给显式注入的协议传输函数。"""

    platform = "onebot11"

    def __init__(self, bot_id: str, sender: ActionSender, *, file_uploader: FileUploader | None = None) -> None:
        """绑定连接身份和动作传输函数。"""
        self.bot_id = _id(bot_id)
        self._sender = sender
        self.message_transform = lambda target, message: message
        self._file_uploader = file_uploader
        self.capabilities = (
            Capabilities()
            .provide(Capability.REPLY, self)
            .provide(Capability.MEDIA, self)
            .provide(Capability.MEDIA_URL, True)
            .provide(Capability.MEMBER_LOOKUP, self)
            .provide(Capability.GROUP_ROLE, self)
            .provide(Capability.GROUP_DIRECTORY, self)
            .provide(Capability.TARGET_LOOKUP, self)
            .provide(Capability.AVATAR, self)
            .provide(Capability.MODERATION, self)
            .provide(Capability.MEMBER_MODERATION, self)
            .provide(Capability.RECALL, self)
            .provide(Capability.FORWARD, self)
            .provide(Capability.FORWARD_SEND, self)
            .provide(Capability.LIKE, self)
            .provide(Capability.VIDEO, self)
            .provide(Capability.QQ_PROFILE, self)
        )
        if file_uploader is not None:
            self.capabilities = self.capabilities.provide(Capability.FILE, self)

    async def call(self, action: str, timeout: float | None = None, **params: Any) -> Any:
        """发送协议动作，网络鉴权与响应关联由传输层负责。"""
        if not isinstance(action, str) or not action:
            raise ValueError("动作名称不能为空")
        data = _copy(params)
        json.dumps(data, allow_nan=False)
        if timeout is None:
            return await self._sender(action, data)
        return await self._sender(action, data, timeout=timeout)

    def avatar_url(self, user_id: str) -> str:
        return f"https://q1.qlogo.cn/g?b=qq&nk={_id(user_id)}&s=640"

    async def send(self, target: SessionTarget, message: Message) -> Any:
        """向同一连接的私聊或群目标发送已编码消息。"""
        if (target.platform, target.bot_id) != (self.platform, self.bot_id):
            raise ValueError("连接与发送目标身份不一致")
        if target.kind not in {"private", "group"}:
            raise UnsupportedCapability("不支持此发送目标")
        key = "group_id" if target.kind == "group" else "user_id"
        action = "send_group_msg" if target.kind == "group" else "send_private_msg"
        return await self.call(
            action,
            **{key: int(_id(target.target_id)), "message": encode_message(self.message_transform(target, message))},
        )

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """按消息 ID 查询原始消息，返回协议数据。"""
        return await self.call("get_msg", message_id=int(_id(message_id, positive=False)))

    async def get_reply(self, event: Event) -> QuotedMessage | None:
        """只接受引用目标和会话都与当前入站事件一致的查询结果。"""
        if event.reply is None:
            return None
        if (event.platform, event.bot_id) != (self.platform, self.bot_id):
            raise ValueError("引用事件不属于当前连接")
        result = await self.get_message(event.reply.message_id)
        if _id(result["message_id"], positive=False) != event.reply.message_id:
            raise ValueError("引用查询返回了不同消息")
        if result["message_type"] != event.target.kind:
            raise ValueError("引用查询返回了不同会话类型")
        if "group_id" in result and _id(result["group_id"]) != event.target.target_id:
            raise ValueError("引用查询返回了其他群的消息")
        sender = result["sender"]
        user_id = _id(sender["user_id"])
        if event.target.kind == "private" and user_id not in {event.user_id, self.bot_id}:
            raise ValueError("引用消息不属于当前私聊")
        name = sender.get("card") or sender.get("nickname") or ""
        if not isinstance(name, str):
            raise ValueError("引用消息作者名称必须是文本")
        return QuotedMessage(event.reply.message_id, user_id, name, parse_message(result["message"]))

    async def resolve_media(self, segment: MessageSegment) -> str | bytes:
        """只返回事件明确提供的可下载来源，不读取协议主机的本地路径。"""
        source = segment.data.get("url") or segment.data.get("file")
        if (
            isinstance(source, bytes)
            or isinstance(source, str)
            and source.startswith(("http://", "https://", "base64://", "data:"))
        ):
            return source
        raise UnsupportedCapability("事件没有可下载媒体来源")

    async def member_name(self, group_id: str, user_id: str) -> str:
        """从群成员查询的确定字段读取展示名称。"""
        member = await self.get_member(group_id, user_id)
        if _id(member["user_id"]) != _id(user_id):
            raise ValueError("成员查询返回了不同用户")
        name = member.get("card") or member.get("nickname") or ""
        if not isinstance(name, str):
            raise ValueError("成员名称必须是文本")
        return name

    async def bot_group_role(self, group_id: str) -> str | None:
        """使用成员查询读取机器人自己的角色。"""
        member = await self.get_member(group_id, self.bot_id)
        if _id(member["user_id"]) != self.bot_id:
            raise ValueError("群角色查询返回了不同用户")
        role = member["role"]
        if role not in {"owner", "admin", "member"}:
            raise ValueError("未知群角色")
        return role

    async def target_name(self, target: SessionTarget) -> str:
        """名称字段和返回身份均按 V11 协议读取。"""
        if (target.platform, target.bot_id) != (self.platform, self.bot_id):
            raise ValueError("会话不属于当前连接")
        if target.kind not in {"group", "private"}:
            raise UnsupportedCapability("不支持此会话名称查询")
        group = target.kind == "group"
        result = await self.get_group(target.target_id) if group else await self.get_user(target.target_id)
        if _id(result["group_id" if group else "user_id"]) != target.target_id:
            raise ValueError("会话查询返回了不同目标")
        name = result["group_name" if group else "nickname"]
        if not isinstance(name, str):
            raise ValueError("会话名称必须是文本")
        return name

    async def get_forward_ids(self, message_id: str) -> list[str]:
        """查询消息后只提取它实际含有的转发 ID。"""
        result = await self.get_message(message_id)
        if _id(result["message_id"], positive=False) != _id(message_id, positive=False):
            raise ValueError("转发查询返回了不同消息")
        message = parse_message(result["message"])
        return [segment.data["id"] for segment in message.segments if segment.type == "forward"]

    async def get_forward_messages(self, forward_id: str) -> list[dict[str, Any]]:
        """按标准的节点数组读取合并转发，不猜测另一种响应结构。"""
        result = await self.call("get_forward_msg", id=forward_id)
        nodes = []
        for segment in result["message"]:
            if segment["type"] != "node":
                raise ValueError("转发响应必须为节点数组")
            node = segment["data"]
            content = parse_message(node["content"])
            nodes.append(
                {
                    "user_id": _id(node["user_id"]),
                    "name": node["nickname"],
                    "text": content.plain_text(),
                    "forwards": [item.data["id"] for item in content.segments if item.type == "forward"],
                }
            )
        return nodes

    async def delete_message(self, message_id: str) -> Any:
        """撤回指定消息。"""
        return await self.call("delete_msg", message_id=int(_id(message_id, positive=False)))

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """查询陌生人信息。"""
        return await self.call("get_stranger_info", user_id=int(_id(user_id)))

    async def get_group(self, group_id: str) -> dict[str, Any]:
        """查询群信息。"""
        return await self.call("get_group_info", group_id=int(_id(group_id)))

    async def get_groups(self) -> list[dict[str, Any]]:
        """查询当前连接加入的群列表。"""
        return await self.call("get_group_list")

    async def get_member(self, group_id: str, user_id: str) -> dict[str, Any]:
        """查询群成员信息。"""
        return await self.call("get_group_member_info", group_id=int(_id(group_id)), user_id=int(_id(user_id)))

    async def member_role(self, group_id: str, user_id: str) -> str:
        member = await self.get_member(group_id, user_id)
        if (
            not isinstance(member, dict)
            or _id(member.get("user_id")) != _id(user_id)
            or member.get("role") not in {"owner", "admin", "member"}
        ):
            raise ValueError("群成员查询身份或角色无效")
        return member["role"]

    async def set_special_title(self, group_id: str, user_id: str, title: str) -> Any:
        return await self.call(
            "set_group_special_title", group_id=int(_id(group_id)), user_id=int(_id(user_id)), special_title=title
        )

    async def like(self, user_id: str, times: int) -> Any:
        if type(times) is not int or not 1 <= times <= 10:
            raise ValueError("点赞次数必须在 1 到 10 之间")
        return await self.call("send_like", user_id=int(_id(user_id)), times=times)

    async def send_forward(self, target: SessionTarget, messages: list[Message]) -> Any:
        if (target.platform, target.bot_id) != (self.platform, self.bot_id):
            raise ValueError("转发目标不属于当前连接")
        if target.kind not in {"group", "private"}:
            raise UnsupportedCapability("不支持此转发目标")
        nodes = [
            {
                "type": "node",
                "data": {
                    "user_id": self.bot_id,
                    "nickname": "FxBot",
                    "content": encode_message(self.message_transform(target, message)),
                },
            }
            for message in messages
        ]
        group = target.kind == "group"
        return await self.call(
            "send_group_forward_msg" if group else "send_private_forward_msg",
            timeout=FORWARD_TIMEOUT,
            **{"group_id" if group else "user_id": int(_id(target.target_id)), "messages": nodes},
        )

    async def get_members(self, group_id: str) -> list[dict[str, Any]]:
        """查询群成员列表。"""
        return await self.call("get_group_member_list", group_id=int(_id(group_id)))

    async def ban(self, group_id: str, user_id: str, duration: int) -> Any:
        """设置群成员禁言时长，零表示解除。"""
        if type(duration) is not int or duration < 0:
            raise ValueError("禁言时长必须为非负整数")
        return await self.call(
            "set_group_ban", group_id=int(_id(group_id)), user_id=int(_id(user_id)), duration=duration
        )

    async def kick(self, group_id: str, user_id: str, *, reject_add_request: bool = False) -> Any:
        """移除群成员。"""
        return await self.call(
            "set_group_kick",
            group_id=int(_id(group_id)),
            user_id=int(_id(user_id)),
            reject_add_request=reject_add_request,
        )

    async def leave_group(self, group_id: str) -> Any:
        """退出指定群。"""
        return await self.call("set_group_leave", group_id=int(_id(group_id)))

    async def set_admin(self, group_id: str, user_id: str, *, enabled: bool) -> Any:
        """设置成员管理员身份。"""
        return await self.call(
            "set_group_admin", group_id=int(_id(group_id)), user_id=int(_id(user_id)), enable=enabled
        )

    async def set_whole_ban(self, group_id: str, *, enabled: bool) -> Any:
        """设置全群禁言。"""
        return await self.call("set_group_whole_ban", group_id=int(_id(group_id)), enable=enabled)

    async def send_file(self, target: SessionTarget, path: Path, name: str) -> Any:
        """文件上传由明确注入的实现策略处理，不发送本机路径给远端主机。"""
        if (target.platform, target.bot_id) != (self.platform, self.bot_id):
            raise ValueError("连接与发送目标身份不一致")
        if target.kind not in {"group", "private"}:
            raise UnsupportedCapability("不支持此文件目标")
        if self._file_uploader is None:
            raise UnsupportedCapability("OneBot V11 标准不定义文件上传动作")
        return await self._file_uploader(target, path, name)
