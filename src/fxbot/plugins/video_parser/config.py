"""视频解析插件配置。"""

from __future__ import annotations

import math
from typing import Any

from ...config import get_manager
from .ui_schema import DEFAULTS


def validate_config(data: dict[str, Any]) -> None:
    for section, values in data.items():
        if section not in DEFAULTS or not isinstance(values, dict):
            raise ValueError("视频配置需要已知分区")
        for key, value in values.items():
            if key not in DEFAULTS[section]:
                raise ValueError("视频配置存在未知设置")
            default = DEFAULTS[section][key]
            if type(default) is int:
                if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"{section}.{key} 必须是有限正数")
            elif type(value) is not type(default):
                raise ValueError(f"视频配置类型错误: {section}.{key}")


REG = get_manager().register("video_parser", DEFAULTS, clean_extra=True, validator=validate_config)


def get_config() -> dict[str, Any]:
    """获取视频解析完整配置。"""
    return REG.load()


def save_config(data: dict[str, Any]) -> None:
    """保存视频解析完整配置。"""
    REG.save(data)


def cfg_general() -> dict[str, Any]:
    """获取通用配置。"""
    return get_config()["general"]


def cfg_platforms() -> dict[str, bool]:
    """获取平台开关。"""
    return get_config()["platforms"]


def cfg_network() -> dict[str, Any]:
    """获取网络配置。"""
    return get_config()["network"]


def cfg_douyin() -> dict[str, Any]:
    """获取抖音请求配置。"""
    return get_config()["douyin"]


def is_global_enabled() -> bool:
    """判断全局解析是否启用。"""
    return cfg_general()["global_enabled"]


def set_global_enabled(enabled: bool) -> None:
    """设置全局解析开关。"""
    data = get_config()
    if type(enabled) is not bool:
        raise ValueError("全局解析开关必须为布尔值")
    data["general"]["global_enabled"] = enabled
    save_config(data)
