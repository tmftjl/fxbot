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
    # 双 Bot 群身份映射：只填 OneBot 群号，映射从两侧消息自动学习。
    "mapping_enabled": False,
    "mapping_groups": [],
}
