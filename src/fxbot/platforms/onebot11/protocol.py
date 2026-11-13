"""OneBot V11 的全双工线协议。"""

from __future__ import annotations

from .codec import _id, parse_event
from ...core import Event
from .client import ActionSender, OneBot11Client
from .napcat import NapCatClient


class OneBot11Protocol:
    """使用明确的 post_type 和 self_id 识别协议帧。"""

    platform = "onebot11"

    def __init__(self, implementation: str = "standard") -> None:
        if implementation not in {"standard", "napcat"}:
            raise ValueError("不支持此 V11 实现类型")
        self.implementation = implementation

    def create_client(self, bot_id: str, sender: ActionSender) -> OneBot11Client:
        """为连接创建直接动作客户端。"""
        client = NapCatClient if self.implementation == "napcat" else OneBot11Client
        return client(bot_id, sender)

    def is_event(self, payload: dict) -> bool:
        """V11 事件使用 post_type 字段。"""
        return "post_type" in payload

    def parse_event(self, payload: dict, bot_id: str) -> Event | None:
        """校验连接身份后解析非元事件。"""
        if _id(payload.get("self_id")) != _id(bot_id):
            raise ValueError("连接身份不一致")
        return None if payload["post_type"] == "meta_event" else parse_event(payload, bot_id=bot_id)

    def encode_action(self, packet: dict, bot_id: str) -> dict:
        """V11 的身份由连接本身指定。"""
        return packet
