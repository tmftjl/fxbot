"""插件配置的唯一默认值声明，不依赖 UI 或配置存储。"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "basic": {
        "enabled": True,
        "groups": [],
        "days": 1,
        "max_days": 7,
        "max_messages": 1000,
        "min_messages": 30,
        "filter_self": True,
        "theme": "miku",
    },
    "auto": {
        "auto_groups": [],
        "auto_hour": 8,
        "auto_minute": 0,
    },
    "analysis_features": {
        "topic_analysis_enabled": True,
        "user_title_analysis_enabled": True,
        "golden_quote_analysis_enabled": True,
        "chat_quality_analysis_enabled": True,
        "max_topics": 5,
        "max_user_titles": 8,
        "max_golden_quotes": 5,
        "keep_original_persona": False,
        "use_plugin_specific_persona": False,
        "plugin_specific_persona_id": "",
    },
}
