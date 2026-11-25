"""Bot 掉线监控插件的默认配置与控制台界面。"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 30,
    "offline_grace_seconds": 60,
    "repeat_minutes": 30,
    "notify_recovery": True,
}


def get_ui_schema() -> dict[str, Any]:
    """返回 Bot 监控配置界面 schema。"""
    return {
        "key": "bot_monitor",
        "title": "Bot 监控",
        "order": 70,
        "cards": [
            {
                "key": "bot_monitor",
                "title": "掉线提醒",
                "schemas": [
                    {
                        "field": "enabled",
                        "label": "启用掉线监控",
                        "component": "Switch",
                        "default": DEFAULTS["enabled"],
                        "helpMessage": "上线过的 Bot 掉线后私聊提醒超级用户；维护期间可临时关闭。",
                    },
                    {
                        "field": "interval_seconds",
                        "label": "检测间隔（秒）",
                        "component": "InputNumber",
                        "default": DEFAULTS["interval_seconds"],
                        "helpMessage": "多久检查一次在线列表。",
                        "componentProps": {"min": 10},
                    },
                    {
                        "field": "offline_grace_seconds",
                        "label": "掉线宽限（秒）",
                        "component": "InputNumber",
                        "default": DEFAULTS["offline_grace_seconds"],
                        "helpMessage": "连续离线超过这个时长才提醒，用来忽略重连抖动。",
                        "componentProps": {"min": 0},
                    },
                    {
                        "field": "repeat_minutes",
                        "label": "重复提醒间隔（分钟）",
                        "component": "InputNumber",
                        "default": DEFAULTS["repeat_minutes"],
                        "helpMessage": "持续掉线时每隔多久再提醒一次，0 表示只提醒一次。",
                        "componentProps": {"min": 0},
                    },
                    {
                        "field": "notify_recovery",
                        "label": "恢复提醒",
                        "component": "Switch",
                        "default": DEFAULTS["notify_recovery"],
                        "helpMessage": "发出过掉线提醒的 Bot 重新上线时再发一条恢复消息；宽限内的短暂重连不提醒。",
                    },
                ],
            }
        ],
    }
