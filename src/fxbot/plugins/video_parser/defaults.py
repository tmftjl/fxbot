"""插件配置的唯一默认值声明，不依赖 UI 或配置存储。"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "general": {
        "global_enabled": True,
        "use_base64": False,
        "send_cover": False,
        "max_file_mb": 80,
        "max_duration_seconds": 600,
        "request_timeout_seconds": 20,
    },
    "platforms": {
        "douyin": True,
        "kuaishou": True,
        "weibo": True,
        "xiaohongshu": True,
        "bilibili": True,
    },
    "douyin": {
        "use_cookie": False,
        "cookie": "",
    },
    "network": {
        "proxy": "http://127.0.0.1:7890",
    },
}
