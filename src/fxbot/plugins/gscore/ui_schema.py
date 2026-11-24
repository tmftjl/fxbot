"""gscore 控制台配置页。"""

from __future__ import annotations

from typing import Any

from .defaults import DEFAULTS


def get_ui_schema() -> dict[str, Any]:
    """返回 gscore 连接器配置界面 schema。"""
    return {
        "key": "gscore",
        "title": "gscore 连接器",
        "cards": [
            {
                "key": "gscore",
                "title": "gsuid_core 连接",
                "schemas": [
                    {
                        "field": "enabled",
                        "label": "启用连接器",
                        "component": "Switch",
                        "default": DEFAULTS["enabled"],
                        "helpMessage": "关闭后不再连接 gsuid_core，入站事件只保留在 FxBot 内。",
                    },
                    {
                        "field": "host",
                        "label": "gsuid_core 地址",
                        "component": "Input",
                        "default": DEFAULTS["host"],
                        "helpMessage": "仅填写主机名或 IP，不包含 ws:// 前缀。",
                    },
                    {
                        "field": "port",
                        "label": "端口",
                        "component": "InputNumber",
                        "default": DEFAULTS["port"],
                        "helpMessage": "gsuid_core WebSocket 端口，默认 8765。",
                        "componentProps": {"min": 1, "max": 65535},
                    },
                    {
                        "field": "token",
                        "label": "WS Token",
                        "component": "InputPassword",
                        "default": DEFAULTS["token"],
                        "helpMessage": "gsuid_core 配置了 WS_TOKEN 时填写同一个令牌，留空表示不鉴权。",
                    },
                    {
                        "field": "bot_id",
                        "label": "连接 Bot ID",
                        "component": "Input",
                        "default": DEFAULTS["bot_id"],
                        "helpMessage": "gsuid_core 用于区分本连接器的名称。",
                    },
                    {
                        "field": "reply_image",
                        "label": "转发引用图片",
                        "component": "Switch",
                        "default": DEFAULTS["reply_image"],
                        "helpMessage": "开启后会把被引用消息中的图片一并上报给 gsuid_core。",
                    },
                    {
                        "field": "mapping_enabled",
                        "label": "双 Bot 身份映射",
                        "component": "Switch",
                        "default": DEFAULTS["mapping_enabled"],
                        "helpMessage": "只在下方列出的双 Bot 群生效：学到映射后，官方 Bot 收到的事件按 OneBot 身份上报，回复仍由官方 Bot 发出。",
                    },
                    {
                        "field": "mapping_groups",
                        "label": "需要映射的 OneBot 群号",
                        "component": "GTags",
                        "default": DEFAULTS["mapping_groups"],
                        "helpMessage": "只填 OneBot 群号（如 757463664）。QQ 官方侧的群 openid 由两侧消息自动配对锁定，需要连续 5 次唯一命中。",
                    },
                ],
            }
        ],
    }
