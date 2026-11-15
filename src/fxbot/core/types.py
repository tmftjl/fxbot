"""FxBot 独立运行时使用的平台无关数据类型和能力接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from datetime import datetime, timezone
from dataclasses import field, dataclass
from urllib.parse import quote, unquote
from collections.abc import Callable


class UnsupportedCapability(RuntimeError):
    """当前平台或客户端不支持请求的能力。"""


if TYPE_CHECKING:
    from .capabilities import Capabilities


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
    """平台只需实现消息发送；可选操作由独立能力提供者负责。"""

    platform: str
    bot_id: str
    capabilities: Capabilities
    message_transform: Callable[[SessionTarget, Message], Message]

    async def send(self, target: SessionTarget, message: Message) -> Any: ...


class ReplySender(Protocol):
    """聊天输出通道，不承担路由处理器的生命周期。"""

    async def send(self, message: Message | str) -> Any:
        """提交一次实际发送并返回回执。"""
