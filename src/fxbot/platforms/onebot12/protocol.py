"""OneBot V12 的平台身份与全双工线协议。"""

from __future__ import annotations

from .codec import parse_event
from ...core import Event
from .client import ActionSender, OneBot12Client


class OneBot12Protocol:
    """把实际平台身份同时放入事件校验和动作帧。"""

    def __init__(self, platform: str) -> None:
        """固定此连接所属的实际平台。"""
        self.upstream = platform
        self.platform = f"onebot12.{platform}"

    def create_client(self, bot_id: str, sender: ActionSender) -> OneBot12Client:
        """为连接创建 V12 客户端。"""
        return OneBot12Client(bot_id, self.upstream, sender)

    def is_event(self, payload: dict) -> bool:
        """V12 事件使用 type 字段。"""
        return "type" in payload

    def parse_event(self, payload: dict, bot_id: str) -> Event | None:
        """元事件不进入业务路由，其余必须匹配平台和身份。"""
        if payload.get("type") == "meta":
            return None
        return parse_event(payload, bot_id=bot_id, platform=self.upstream)

    def encode_action(self, packet: dict, bot_id: str) -> dict:
        """V12 动作显式携带自身平台标识。"""
        return {**packet, "self": {"platform": self.upstream, "user_id": bot_id}}
