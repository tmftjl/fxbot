"""FxBot 独立运行时使用的平台无关数据类型和能力接口。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import field, dataclass
from urllib.parse import quote, unquote
from collections.abc import Callable


class UnsupportedCapability(RuntimeError):
    """当前平台或客户端不支持请求的能力。"""


class Capability(Enum):
    """平台明确声明的可选能力，不通过失败探测猜测。"""

    REPLY = "reply"
    MEMBER_LOOKUP = "member_lookup"
    REFERENCE_LOOKUP = "reference_lookup"
    FORWARD = "forward"
    FILE = "file"
    GROUP_ROLE = "group_role"
    TARGET_LOOKUP = "target_lookup"
    HISTORY = "history"
    AVATAR = "avatar"
    MODERATION = "moderation"
    ESSENCE = "essence"
    MUTED_MEMBERS = "muted_members"
    FORWARD_SEND = "forward_send"
    LIKE = "like"
    VIDEO = "video"
    QQ_PROFILE = "qq_profile"


@dataclass(slots=True, frozen=True)
class MessageSegment:
    """一段平台无关的入站或出站消息。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text(cls, value: str) -> "MessageSegment":
        """创建文本段。"""
        return cls("text", {"text": str(value)})

    @classmethod
    def mention(cls, user_id: str, name: str = "") -> "MessageSegment":
        """创建提及段。"""
        return cls("mention", {"user_id": str(user_id), "name": str(name)})

    @classmethod
    def image(cls, source: str | bytes) -> "MessageSegment":
        """声明图片来源，由平台客户端完成上传或编码。"""
        return cls("image", {"file": source})

    @classmethod
    def audio(cls, source: str | bytes) -> "MessageSegment":
        """声明语音来源。"""
        return cls("audio", {"file": source})

    @classmethod
    def voice(cls, source: str | bytes) -> "MessageSegment":
        """声明语音消息而非独立音频文件。"""
        return cls("voice", {"file": source})

    @classmethod
    def reply(cls, message_id: str) -> "MessageSegment":
        """构造明确引用的消息段。"""
        return cls("reply", {"message_id": message_id})

    def text_value(self) -> str:
        """返回文本段内容，非文本段返回空字符串。"""
        return str(self.data.get("text", "")) if self.type == "text" else ""


@dataclass(slots=True)
class Message:
    """可变的平台无关消息段集合。"""

    segments: list[MessageSegment] = field(default_factory=list)

    @classmethod
    def text(cls, value: str) -> "Message":
        """创建只包含一个文本段的消息。"""
        return cls([MessageSegment.text(value)])

    def plain_text(self) -> str:
        """拼接所有文本段。"""
        return "".join(segment.text_value() for segment in self.segments)

    def mentions(self) -> list[str]:
        """返回消息中提及的用户 ID。"""
        return [
            str(segment.data["user_id"])
            for segment in self.segments
            if segment.type == "mention" and segment.data.get("user_id") is not None
        ]


@dataclass(slots=True, frozen=True)
class SessionTarget:
    """标识一条消息所属的持久化会话。"""

    platform: str
    bot_id: str
    kind: str
    target_id: str

    def __post_init__(self) -> None:
        """拒绝缺失的身份和无法识别的目标类型。"""
        if not all(isinstance(value, str) and value for value in (self.platform, self.bot_id, self.target_id)):
            raise ValueError("会话目标必须包含非空的平台、Bot 和目标 ID")
        if self.kind not in {"private", "group", "guild", "channel"}:
            raise ValueError("不支持的会话目标类型")

    def key(self) -> str:
        """生成不会混淆平台和目标类型的稳定键。"""
        return ":".join(quote(value, safe="") for value in (self.platform, self.bot_id, self.kind, self.target_id))

    def bot_key(self) -> str:
        """生成不会混淆平台名和机器人 ID 的连接键。"""
        return ":".join(quote(value, safe="") for value in (self.platform, self.bot_id))

    @classmethod
    def from_key(cls, key: str) -> "SessionTarget":
        """解析规范会话键，拒绝缺失平台身份的历史裸 ID。"""
        parts = key.split(":")
        if len(parts) != 4:
            raise ValueError("会话键必须明确包含平台、机器人、类型和目标")
        target = cls(*(unquote(part) for part in parts))
        if target.key() != key:
            raise ValueError("会话键不是规范编码")
        return target


@dataclass(slots=True, frozen=True)
class ReplyTarget:
    """表示消息引用的目标。"""

    message_id: str
    user_id: str | None = None
    user_name: str = ""


@dataclass(slots=True, frozen=True)
class HistoryMessage:
    """历史统计所需的规范消息，不携带协议客户端或原始框架事件。"""

    message_id: str
    timestamp: int
    user_id: str
    user_name: str = ""
    text: str = ""
    images: int = 0
    reply_to_id: str = ""


@dataclass(slots=True, frozen=True)
class QuotedMessage:
    """平台查询得到的引用正文及真实作者。"""

    message_id: str
    sender_id: str | None
    sender_name: str
    message: Message = field(default_factory=Message)


@dataclass(slots=True, frozen=True)
class Event:
    """FxBot 核心收到的一条平台无关事件。"""

    event_id: str | None
    message_id: str | None
    event_type: str
    platform: str
    bot_id: str
    user_id: str
    user_name: str
    target: SessionTarget
    message: Message = field(default_factory=Message)
    reply: ReplyTarget | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    to_me: bool = False
    original_message: Message | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """事件和发送目标必须属于同一平台及 Bot。"""
        if (self.platform, self.bot_id) != (self.target.platform, self.target.bot_id):
            raise ValueError("事件与会话目标的 Bot 身份不一致")
        if self.occurred_at.tzinfo is None:
            raise ValueError("事件发生时间必须明确包含时区")

    @property
    def is_message(self) -> bool:
        """判断事件是否是消息事件。"""
        return self.event_type == "message"

    @property
    def is_group(self) -> bool:
        """判断事件是否属于群会话。"""
        return self.target.kind == "group"


@runtime_checkable
class BotClient(Protocol):
    """平台连接必须提供的最小客户端能力。"""

    platform: str
    bot_id: str
    capabilities: frozenset[Capability]
    message_transform: Callable[[SessionTarget, Message], Message]

    async def send(self, target: SessionTarget, message: Message) -> Any:
        """向目标发送消息。"""

    async def call(self, action: str, **params: Any) -> Any:
        """调用平台能力；不支持时抛出 UnsupportedCapability。"""

    async def get_groups(self) -> list[dict[str, Any]]:
        """查询当前连接的群列表。"""

    async def member_role(self, group_id: str, user_id: str) -> str:
        """查询并验证群成员真实身份，不把未知身份当成管理员。"""

    async def get_member(self, group_id: str, user_id: str) -> dict[str, Any]:
        """查询当前连接的群成员。"""

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """查询平台用户资料。"""

    async def ban(self, group_id: str, user_id: str, duration: int) -> Any:
        """禁言或以零时长解除禁言。"""

    async def kick(self, group_id: str, user_id: str, *, reject_add_request: bool = False) -> Any:
        """从当前群移出成员。"""

    async def set_admin(self, group_id: str, user_id: str, *, enabled: bool) -> Any:
        """调整成员管理员身份。"""

    async def set_whole_ban(self, group_id: str, *, enabled: bool) -> Any:
        """调整全群禁言。"""

    async def set_special_title(self, group_id: str, user_id: str, title: str) -> Any:
        """设置平台群头衔。"""

    async def delete_message(self, message_id: str) -> Any:
        """撤回当前平台的消息。"""

    async def set_essence(self, message_id: str, *, enabled: bool) -> Any:
        """设置或取消精华消息。"""

    async def get_muted_members(self, group_id: str) -> list[dict[str, Any]]:
        """读取平台明确提供的禁言成员及到期时刻。"""

    async def like(self, user_id: str, times: int) -> Any:
        """平台好友点赞。"""

    async def send_forward(self, target: SessionTarget, messages: list[Message]) -> Any:
        """发送合并转发节点，平台不支持时明确拒绝。"""

    async def leave_group(self, group_id: str) -> Any:
        """退出当前平台的指定群。"""

    async def get_reply(self, event: Event) -> QuotedMessage | None:
        """读取此事件已引用的消息，不推测其他会话的目标。"""

    async def resolve_media(self, segment: MessageSegment) -> str | bytes:
        """将协议文件引用解析为明确可读取的媒体来源。"""

    async def member_name(self, group_id: str, user_id: str) -> str:
        """查询当前平台成员名称。"""

    async def target_name(self, target: SessionTarget) -> str:
        """查询此连接所属会话的展示名称。"""

    async def fetch_group_history(
        self, target: SessionTarget, *, start_time: int, end_time: int, limit: int
    ) -> list[HistoryMessage]:
        """读取明确群会话的时间窗口；失败不得作为空历史返回。"""

    def avatar_url(self, user_id: str) -> str:
        """返回平台明确支持的成员头像地址。"""

    async def bot_group_role(self, group_id: str) -> str | None:
        """查询机器人自己的群角色；不支持的平台明确拒绝。"""

    async def send_file(self, target: SessionTarget, path: Path, name: str) -> Any:
        """向明确会话发送本地生成的文件。"""

    async def get_forward_messages(self, forward_id: str) -> list[dict[str, Any]]:
        """读取平台明确支持的合并转发内容。"""

    async def get_forward_ids(self, message_id: str) -> list[str]:
        """从指定消息查找转发 ID，不将查询失败当成另一种 ID。"""


class ReplySender(Protocol):
    """聊天输出通道，不承担路由处理器的生命周期。"""

    async def send(self, message: Message | str) -> Any:
        """提交一次实际发送并返回回执。"""
