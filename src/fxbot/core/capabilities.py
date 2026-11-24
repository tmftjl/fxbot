"""独立可选能力协议及其显式、不可变的提供者注册表。"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Generic, TypeVar, Protocol, cast
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Mapping

from .types import Event, Message, QuotedMessage, SessionTarget, HistoryMessage, MessageSegment, UnsupportedCapability

T = TypeVar("T")


class MessageSender(Protocol):
    async def send(self, target: SessionTarget, message: Message) -> Any: ...


class ReplyReader(Protocol):
    async def get_reply(self, event: Event) -> QuotedMessage | None: ...


class MediaResolver(Protocol):
    async def resolve_media(self, segment: MessageSegment) -> str | bytes: ...


class MemberLookup(Protocol):
    async def get_member(self, group_id: str, user_id: str) -> dict[str, Any]: ...
    async def get_members(self, group_id: str) -> list[dict[str, Any]]: ...
    async def member_name(self, group_id: str, user_id: str) -> str: ...


class GroupRoles(Protocol):
    async def member_role(self, group_id: str, user_id: str) -> str: ...
    async def bot_group_role(self, group_id: str) -> str | None: ...


class GroupDirectory(Protocol):
    async def get_groups(self) -> list[dict[str, Any]]: ...
    async def leave_group(self, group_id: str) -> Any: ...


class UserLookup(Protocol):
    async def get_user(self, user_id: str) -> dict[str, Any]: ...


class TargetLookup(Protocol):
    async def target_name(self, target: SessionTarget) -> str: ...


class HistoryReader(Protocol):
    async def fetch_group_history(
        self, target: SessionTarget, *, start_time: int, end_time: int, limit: int
    ) -> list[HistoryMessage]: ...


class AvatarProvider(Protocol):
    def avatar_url(self, user_id: str) -> str: ...


class MemberModeration(Protocol):
    async def ban(self, group_id: str, user_id: str, duration: int) -> Any: ...
    async def kick(self, group_id: str, user_id: str, *, reject_add_request: bool = False) -> Any: ...


class Moderation(MemberModeration, Protocol):
    async def set_admin(self, group_id: str, user_id: str, *, enabled: bool) -> Any: ...
    async def set_whole_ban(self, group_id: str, *, enabled: bool) -> Any: ...
    async def set_special_title(self, group_id: str, user_id: str, title: str) -> Any: ...


class Recall(Protocol):
    async def delete_message(self, message_id: str) -> Any: ...


class Essence(Protocol):
    async def set_essence(self, message_id: str, *, enabled: bool) -> Any: ...


class MutedMembers(Protocol):
    async def get_muted_members(self, group_id: str) -> list[dict[str, Any]]: ...


class LikeSender(Protocol):
    async def like(self, user_id: str, times: int) -> Any: ...


class ForwardSender(Protocol):
    async def send_forward(self, target: SessionTarget, messages: list[Message]) -> Any: ...


class ForwardReader(Protocol):
    async def get_forward_ids(self, message_id: str) -> list[str]: ...
    async def get_forward_messages(self, forward_id: str) -> list[dict[str, Any]]: ...


class FileSender(Protocol):
    async def send_file(self, target: SessionTarget, path: Path, name: str) -> Any: ...


@dataclass(frozen=True, slots=True, eq=False)
class CapabilityKey(Generic[T]):
    """键的类型决定 require 返回的接口，不做反射探测或失败重试。"""

    name: str


class Capability:
    REPLY = CapabilityKey[ReplyReader]("reply")
    MEDIA = CapabilityKey[MediaResolver]("media")
    # 平台接受远程 HTTP(S) 媒体来源并由平台侧代下；缺省时应由插件先下载。
    MEDIA_URL = CapabilityKey[bool]("media_url")
    MEMBER_LOOKUP = CapabilityKey[MemberLookup]("member_lookup")
    GROUP_ROLE = CapabilityKey[GroupRoles]("group_role")
    GROUP_DIRECTORY = CapabilityKey[GroupDirectory]("group_directory")
    TARGET_LOOKUP = CapabilityKey[TargetLookup]("target_lookup")
    HISTORY = CapabilityKey[HistoryReader]("history")
    AVATAR = CapabilityKey[AvatarProvider]("avatar")
    MODERATION = CapabilityKey[Moderation]("moderation")
    MEMBER_MODERATION = CapabilityKey[MemberModeration]("member_moderation")
    RECALL = CapabilityKey[Recall]("recall")
    ESSENCE = CapabilityKey[Essence]("essence")
    MUTED_MEMBERS = CapabilityKey[MutedMembers]("muted_members")
    LIKE = CapabilityKey[LikeSender]("like")
    FORWARD_SEND = CapabilityKey[ForwardSender]("forward_send")
    FORWARD = CapabilityKey[ForwardReader]("forward")
    FILE = CapabilityKey[FileSender]("file")
    VIDEO = CapabilityKey[MessageSender]("video")
    QQ_PROFILE = CapabilityKey[UserLookup]("qq_profile")


class Capabilities:
    """平台仅提供真实实现，调用方显式取得所需的窄接口。"""

    def __init__(self) -> None:
        self._providers: Mapping[object, object] = MappingProxyType({})

    def provide(self, key: CapabilityKey[T], provider: T) -> Capabilities:
        if key in self._providers:
            raise ValueError(f"能力已注册: {key.name}")
        result = Capabilities()
        result._providers = MappingProxyType({**self._providers, key: provider})
        return result

    def __contains__(self, key: object) -> bool:
        return key in self._providers

    def require(self, key: CapabilityKey[T]) -> T:
        try:
            provider = self._providers[key]
        except KeyError:
            raise UnsupportedCapability(f"当前平台未提供 {key.name} 能力") from None
        return cast(T, provider)
