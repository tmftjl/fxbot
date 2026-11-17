"""表情包插件配置默认值；配置与 UI 共用这一份声明。"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "base_url": "http://127.0.0.1:2233",
    "command_prefixes": None,
    "disabled_list": [],
    "check_resources_on_startup": True,
    "random_meme_show_info": True,
    "notice_prob": 0.1,
    "use_gif": False,
    "use_ban_word": True,
    "whitelist_ids": [],
    "protected_memes": [],
}
