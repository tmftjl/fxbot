"""OneBot V12 适配器：bot_id 固定为 ``onebot_v12``，匹配全部 onebot12 平台。"""

from __future__ import annotations

from fxbot.core import Event

from .base import CoreAdapter


class OneBot12Adapter(CoreAdapter):
    key = "onebot12"
    name = "OneBot V12"

    def platform_matches(self, platform: str) -> bool:
        return platform.startswith("onebot12.")

    def matches_send(self, bot_id: str) -> bool:
        return bot_id == "onebot_v12"

    def receive_bot_id(self, event: Event) -> str:
        return "onebot_v12"
