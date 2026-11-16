"""QQ 官方开放平台的独立协议实现。"""

from .config import QQBotSettings
from .actions import QQ_API, QQActions

__all__ = ["QQ_API", "QQActions", "QQBotSettings"]
