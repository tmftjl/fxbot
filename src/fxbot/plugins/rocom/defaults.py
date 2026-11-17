"""插件配置的唯一默认值声明，不依赖 UI 或配置存储。"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "merchant": {
        "enabled": True,
        "push_hours": [8, 12, 16, 20],
        "push_minute": 5,
        "retry_times": 20,
        "retry_interval_seconds": 30,
    },
    "resources": {
        "enabled": True,
        "base_url": "",
        "concurrency": 12,
    },
}
