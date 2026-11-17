"""洛克王国插件控制台配置。"""

from __future__ import annotations

from typing import Any

from .defaults import DEFAULTS


def get_ui_schema() -> dict[str, Any]:
    """返回洛克王国插件配置界面 schema。"""
    return {
        "key": "rocom",
        "title": "洛克王国配置",
        "cards": [
            {
                "key": "rocom.merchant",
                "title": "远行商人推送",
                "schemas": [
                    {
                        "field": "enabled",
                        "label": "启用定时推送",
                        "component": "Switch",
                        "default": DEFAULTS["merchant"]["enabled"],
                        "helpMessage": "关闭后不再检查并推送远行商人刷新。",
                    },
                    {
                        "field": "push_hours",
                        "label": "推送小时",
                        "component": "GArrayInput",
                        "default": DEFAULTS["merchant"]["push_hours"],
                        "helpMessage": "每天在这些整点后的指定分钟检查刷新，默认 8/12/16/20 点。",
                    },
                    {
                        "field": "push_minute",
                        "label": "推送分钟",
                        "component": "InputNumber",
                        "default": DEFAULTS["merchant"]["push_minute"],
                        "helpMessage": "每次检查的分钟数，默认 5 分。",
                        "componentProps": {"min": 0, "max": 59},
                    },
                    {
                        "field": "retry_times",
                        "label": "刷新重试次数",
                        "component": "InputNumber",
                        "default": DEFAULTS["merchant"]["retry_times"],
                        "helpMessage": "刷新点后最多检查多少次，直到拿到新商品。",
                        "componentProps": {"min": 1, "max": 100},
                    },
                    {
                        "field": "retry_interval_seconds",
                        "label": "重试间隔秒",
                        "component": "InputNumber",
                        "default": DEFAULTS["merchant"]["retry_interval_seconds"],
                        "helpMessage": "两次检查之间的等待时间。",
                        "componentProps": {"min": 1, "max": 600},
                    },
                ],
            },
            {
                "key": "rocom.resources",
                "title": "运行时资源",
                "schemas": [
                    {
                        "field": "enabled",
                        "label": "允许资源下载",
                        "component": "Switch",
                        "default": DEFAULTS["resources"]["enabled"],
                        "helpMessage": "关闭后手动下载命令不会请求任何资源站。",
                    },
                    {
                        "field": "base_url",
                        "label": "指定资源站",
                        "component": "Input",
                        "default": DEFAULTS["resources"]["base_url"],
                        "helpMessage": "留空时自动探测内置 GsCore 资源站。",
                    },
                    {
                        "field": "concurrency",
                        "label": "下载并发",
                        "component": "InputNumber",
                        "default": DEFAULTS["resources"]["concurrency"],
                        "helpMessage": "单个目录内同时下载的文件数。",
                        "componentProps": {"min": 1, "max": 32},
                    },
                ],
            },
        ],
    }
