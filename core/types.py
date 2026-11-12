"""FxBot 独立运行时使用的平台无关数据类型和能力接口。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from dataclasses import field, dataclass
from urllib.parse import quote


class UnsupportedCapability(RuntimeError):
    """当前平台或客户端不支持请求的能力。"""


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


@dataclass(slots=True, frozen=True)
class ReplyTarget:
    """表示消息引用的目标。"""

    message_id: str
    user_id: str | None = None
    user_name: str = ""


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

    def __post_init__(self) -> None:
        """事件和发送目标必须属于同一平台及 Bot。"""
        if (self.platform, self.bot_id) != (self.target.platform, self.target.bot_id):
            raise ValueError("事件与会话目标的 Bot 身份不一致")

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

    async def send(self, target: SessionTarget, message: Message) -> Any:
        """向目标发送消息。"""

    async def call(self, action: str, **params: Any) -> Any:
        """调用平台能力；不支持时抛出 UnsupportedCapability。"""
