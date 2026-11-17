"""洛克王国插件配置。"""

from __future__ import annotations

from typing import Any

from ...config import get_manager
from .defaults import DEFAULTS


def _validate_bool(value: Any, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} 必须是布尔值")


def _validate_int(value: Any, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")


def validate_config(data: dict[str, Any]) -> None:
    """校验局部或合并后的配置，拒绝字符串开关和越界计划。"""
    for section, values in data.items():
        if section not in DEFAULTS or not isinstance(values, dict):
            raise ValueError("洛克王国配置需要已知分区")
        for key in values:
            if key not in DEFAULTS[section]:
                raise ValueError(f"洛克王国配置存在未知设置: {section}.{key}")

    merchant = data.get("merchant") or {}
    if "enabled" in merchant:
        _validate_bool(merchant["enabled"], "merchant.enabled")
    if "push_hours" in merchant:
        hours = merchant["push_hours"]
        if not isinstance(hours, list) or not hours:
            raise ValueError("merchant.push_hours 必须是非空小时数组")
        for hour in hours:
            if type(hour) is int:
                value = hour
            elif isinstance(hour, str) and hour.isascii() and hour.isdecimal():
                value = int(hour)
            else:
                raise ValueError("merchant.push_hours 的小时必须是整数")
            if not 0 <= value <= 23:
                raise ValueError("merchant.push_hours 的小时必须在 0 到 23 之间")
    if "push_minute" in merchant:
        _validate_int(merchant["push_minute"], "merchant.push_minute", 0, 59)
    if "retry_times" in merchant:
        _validate_int(merchant["retry_times"], "merchant.retry_times", 1, 100)
    if "retry_interval_seconds" in merchant:
        _validate_int(merchant["retry_interval_seconds"], "merchant.retry_interval_seconds", 1, 600)

    resources = data.get("resources") or {}
    if "enabled" in resources:
        _validate_bool(resources["enabled"], "resources.enabled")
    if "base_url" in resources:
        base_url = resources["base_url"]
        if not isinstance(base_url, str) or (base_url and not base_url.startswith(("http://", "https://"))):
            raise ValueError("resources.base_url 必须为空或 http(s) 地址")
    if "concurrency" in resources:
        _validate_int(resources["concurrency"], "resources.concurrency", 1, 32)


REG = get_manager().register("rocom", DEFAULTS, clean_extra=True, validator=validate_config)


def get_config() -> dict[str, Any]:
    """获取洛克王国完整配置。"""
    return REG.load()


def merchant_config() -> dict[str, Any]:
    """获取远行商人推送配置。"""
    return get_config()["merchant"]


def resources_config() -> dict[str, Any]:
    """获取运行时资源下载配置。"""
    return get_config()["resources"]
