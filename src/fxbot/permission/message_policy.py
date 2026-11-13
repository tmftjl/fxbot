"""消息准入：忽略明确配置的其他机器人提及。"""

from __future__ import annotations

from ..core import Event, SessionTarget
from ..config import ConfigManager


class MessagePolicy:
    """按连接命名空间匹配提及，不复用不同平台的裸 ID。"""

    def __init__(self, configuration: ConfigManager) -> None:
        self.configuration = configuration

    async def __call__(self, event: Event) -> bool:
        ignored = self.configuration.get_system()["system"]["ignored_mention_bot_ids"]
        mentions = (event.original_message or event.message).mentions()
        return not any(
            SessionTarget(event.platform, event.bot_id, "private", user_id).key() in ignored for user_id in mentions
        )
