"""OneBot V11 的全双工线协议。"""

from __future__ import annotations

from .codec import _id, parse_event
from ...core import Event
from .client import ActionSender, OneBot11Client
from .extensions import OneBot11ExtendedClient


class OneBot11Protocol:
    """使用明确的 post_type 和 self_id 识别协议帧。"""

    platform = "onebot11"

    def create_client(self, bot_id: str, sender: ActionSender) -> OneBot11Client:
        """V11 连接统一提供扩展动作；历史分页方言在首次查询时探测。"""
        return OneBot11ExtendedClient(bot_id, sender)

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
