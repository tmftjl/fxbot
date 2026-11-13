"""OneBot V12 的直接动作客户端。"""

from __future__ import annotations

import base64
from typing import Any
from pathlib import Path
from collections.abc import Callable, Awaitable

from .codec import _text, _json_copy, encode_message
from ...core import Event, Message, Capability, QuotedMessage, SessionTarget, MessageSegment, UnsupportedCapability

ActionSender = Callable[[str, dict[str, Any]], Awaitable[Any]]


class OneBot12Client:
    """直接使用 OneBot V12 动作，不调用宿主框架对象。"""

    capabilities = frozenset({Capability.REPLY, Capability.MEMBER_LOOKUP, Capability.FILE, Capability.TARGET_LOOKUP})

    def __init__(self, bot_id: str, platform: str, sender: ActionSender) -> None:
        """绑定协议版本、实际平台和机器人身份。"""
        self.bot_id = _text(bot_id)
        self.upstream_platform = _text(platform)
        self.platform = f"onebot12.{platform}"
        self._sender = sender
        self.message_transform = lambda target, message: message

    async def call(self, action: str, **params: Any) -> Any:
        """提交协议动作。"""
        return await self._sender(_text(action), _json_copy(params))

    async def send(self, target: SessionTarget, message: Message) -> Any:
        """向当前连接所属的私聊或群聊发送消息。"""
        self._check_target(target)
        message = self.message_transform(target, message)
        segments = []
        for segment in message.segments:
            if segment.type in {"image", "voice", "audio", "video", "file"} and "file" in segment.data:
                source = segment.data["file"]
                if isinstance(source, bytes):
                    uploaded = await self.call(
                        "upload_file", type="data", name=segment.type, data=base64.b64encode(source).decode()
                    )
                elif isinstance(source, str) and source.startswith(("http://", "https://")):
                    uploaded = await self.call("upload_file", type="url", name=segment.type, url=source)
                else:
                    raise ValueError("出站媒体必须是明确的字节或 HTTP(S) 来源")
                segment = MessageSegment(segment.type, {"file_id": _text(uploaded["file_id"])})
            segments.append(segment)
        key = "group_id" if target.kind == "group" else "user_id"
        return await self.call(
            "send_message",
            detail_type=target.kind,
            **{key: target.target_id},
            message=encode_message(Message(segments)),
        )

    async def resolve_media(self, segment: MessageSegment) -> str:
        """通过标准文件动作获取该连接可访问的 URL。"""
        result = await self.call("get_file", file_id=_text(segment.data["file_id"]), type="url")
        return _text(result["url"])

    async def get_reply(self, event: Event) -> QuotedMessage | None:
        """V12 标准没有引用正文查询，不伪造不存在的动作。"""
        if event.reply is None:
            return None
        if (event.platform, event.bot_id) != (self.platform, self.bot_id):
            raise ValueError("引用事件不属于当前连接")
        return QuotedMessage(event.reply.message_id, event.reply.user_id, event.reply.user_name)

    async def member_name(self, group_id: str, user_id: str) -> str:
        """读取 V12 标准成员名称字段。"""
        member = await self.get_member(group_id, user_id)
        if member["user_id"] != user_id:
            raise ValueError("成员查询返回了不同用户")
        name = member["user_displayname"] or member["user_name"]
        if not isinstance(name, str):
            raise ValueError("成员名称必须是文本")
        return name

    async def get_forward_ids(self, message_id: str) -> list[str]:
        """V12 标准没有消息查询接口。"""
        raise UnsupportedCapability("此协议不支持转发消息查询")

    async def fetch_group_history(self, target: SessionTarget, *, start_time: int, end_time: int, limit: int) -> list:
        raise UnsupportedCapability("OneBot V12 标准不定义群历史查询")

    def avatar_url(self, user_id: str) -> str:
        raise UnsupportedCapability("OneBot V12 标准不定义成员头像")

    async def target_name(self, target: SessionTarget) -> str:
        """读取标准会话名称，不使用其他平台的字段别名。"""
        self._check_target(target)
        group = target.kind == "group"
        result = await self.get_group(target.target_id) if group else await self.get_user(target.target_id)
        if result["group_id" if group else "user_id"] != target.target_id:
            raise ValueError("会话查询返回了不同目标")
        name = result["group_name" if group else "user_name"]
        if not isinstance(name, str):
            raise ValueError("会话名称必须是文本")
        return name

    async def bot_group_role(self, group_id: str) -> str | None:
        """V12 标准未定义群角色。"""
        raise UnsupportedCapability("此协议不支持群角色查询")

    async def member_role(self, group_id: str, user_id: str) -> str:
        raise UnsupportedCapability("OneBot V12 标准不定义群成员角色")

    async def ban(self, group_id: str, user_id: str, duration: int) -> Any:
        raise UnsupportedCapability("OneBot V12 标准不定义禁言")

    async def kick(self, group_id: str, user_id: str, *, reject_add_request: bool = False) -> Any:
        raise UnsupportedCapability("OneBot V12 标准不定义移出成员")

    async def set_admin(self, group_id: str, user_id: str, *, enabled: bool) -> Any:
        raise UnsupportedCapability("OneBot V12 标准不定义调整管理员")

    async def set_whole_ban(self, group_id: str, *, enabled: bool) -> Any:
        raise UnsupportedCapability("OneBot V12 标准不定义全群禁言")

    async def set_special_title(self, group_id: str, user_id: str, title: str) -> Any:
        raise UnsupportedCapability("OneBot V12 标准不定义头衔")

    async def set_essence(self, message_id: str, *, enabled: bool) -> Any:
        raise UnsupportedCapability("OneBot V12 标准不定义精华消息")

    async def get_muted_members(self, group_id: str) -> list[dict[str, Any]]:
        raise UnsupportedCapability("OneBot V12 标准不定义禁言成员查询")

    async def like(self, user_id: str, times: int) -> Any:
        raise UnsupportedCapability("OneBot V12 标准不定义点赞")

    async def send_forward(self, target: SessionTarget, messages: list[Message]) -> Any:
        raise UnsupportedCapability("OneBot V12 标准不定义合并转发发送")

    async def get_forward_messages(self, forward_id: str) -> list[dict[str, Any]]:
        """V12 标准没有合并转发接口。"""
        raise UnsupportedCapability("此协议不支持合并转发")

    def _check_target(self, target: SessionTarget) -> None:
        """发送前验证连接和会话命名空间。"""
        if (target.platform, target.bot_id) != (self.platform, self.bot_id):
            raise ValueError("连接与发送目标身份不一致")
        if target.kind not in {"group", "private"}:
            raise UnsupportedCapability("不支持此发送目标")

    async def delete_message(self, message_id: str) -> Any:
        """删除消息。"""
        return await self.call("delete_message", message_id=_text(message_id))

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """查询用户。"""
        return await self.call("get_user_info", user_id=_text(user_id))

    async def get_group(self, group_id: str) -> dict[str, Any]:
        """查询群资料。"""
        return await self.call("get_group_info", group_id=_text(group_id))

    async def get_groups(self) -> list[dict[str, Any]]:
        """查询群列表。"""
        return await self.call("get_group_list")

    async def leave_group(self, group_id: str) -> Any:
        """退出指定群。"""
        return await self.call("leave_group", group_id=_text(group_id))

    async def get_member(self, group_id: str, user_id: str) -> dict[str, Any]:
        """查询群成员。"""
        return await self.call("get_group_member_info", group_id=_text(group_id), user_id=_text(user_id))

    async def get_members(self, group_id: str) -> list[dict[str, Any]]:
        """查询群成员列表。"""
        return await self.call("get_group_member_list", group_id=_text(group_id))

    async def send_file(self, target: SessionTarget, path: Path, name: str) -> Any:
        """上传文件数据后发送已取得的文件 ID。"""
        self._check_target(target)
        uploaded = await self.call(
            "upload_file", type="data", name=name, data=base64.b64encode(path.read_bytes()).decode()
        )
        file_id = _text(uploaded["file_id"])
        return await self.send(target, Message([MessageSegment("file", {"file_id": file_id})]))
