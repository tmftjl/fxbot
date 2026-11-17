"""gscore 连接器配置的唯一默认值声明。"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "host": "localhost",
    "port": 8765,
    "token": "",
    "bot_id": "FxBot",
    "reply_image": True,
}
