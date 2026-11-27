"""插件配置的唯一默认值声明，不依赖 UI 或配置存储。"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "music": {
        "api_base": "http://127.0.0.1:3000",
        "provider_default": "tencent",
        "search_num": 20,
        "login_mode": "shared",
        "qq_quality": 2,
        "netease_quality": 4,
    },
    "box": {
        "only_admin": False,
        "box_blacklist": [],
        "increase_box": False,
        "auto_box_groups": [],
        "avatar_api_url": "https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640",
    },
    "api_urls": {
        "sick_quote_api": "https://oiapi.net/API/SickL/",
        "doro_api": "https://doro-api.hxxn.cc/get",
        "background_api": "http://127.0.0.1:1520/api/wuthering_waves/role_image/random",
        "gacha_pool_api": "https://wh.loping151.site/api/waves/pool/list",
    },
    "image_display": {
        "enabled": True,
        "type": 2,
        "text": "Ciallo~",
        "list": [
            "你干嘛~",
            "我喜欢你",
            "[图片]",
        ],
        "api": "https://v1.hitokoto.cn/?encode=text",
    },
}
