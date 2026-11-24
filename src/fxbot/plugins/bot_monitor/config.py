"""Bot 掉线监控插件配置。"""

from __future__ import annotations

import math
from typing import Any

from ...config import get_manager
from .ui_schema import DEFAULTS


def validate_config(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("Bot 监控配置必须是对象")
    for key, value in data.items():
        if key not in DEFAULTS:
            raise ValueError("Bot 监控配置存在未知设置")
        default = DEFAULTS[key]
        if type(default) is bool:
            if type(value) is not bool:
                raise ValueError(f"Bot 监控开关类型错误: {key}")
        elif type(default) is int:
            if type(value) not in {int, float} or not math.isfinite(value):
                raise ValueError(f"Bot 监控数值必须是有限数: {key}")
            if value < 0 or (key == "interval_seconds" and value <= 0):
                raise ValueError(f"Bot 监控数值超出范围: {key}")


REG = get_manager().register("bot_monitor", DEFAULTS, clean_extra=True, validator=validate_config)


def get_config() -> dict[str, Any]:
    """读取 Bot 监控完整配置。"""
    return REG.load()
