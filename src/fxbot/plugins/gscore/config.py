"""gscore 配置注册、校验与读取。"""

from __future__ import annotations

from typing import Any

from ...config import get_manager
from .defaults import DEFAULTS


def validate_config(data: dict[str, Any]) -> None:
    """只接受声明过的字段和明确类型，避免连接参数被静默猜成其他值。"""
    if not isinstance(data, dict):
        raise ValueError("gscore 配置必须为对象")
    for key, value in data.items():
        if key not in DEFAULTS:
            raise ValueError(f"gscore 配置包含未知字段: {key}")
        if type(value) is not type(DEFAULTS[key]):
            raise ValueError(f"gscore 配置类型错误: {key}")
    if "host" in data and not data["host"].strip():
        raise ValueError("gscore host 不能为空")
    if "bot_id" in data and not data["bot_id"].strip():
        raise ValueError("gscore bot_id 不能为空")
    if "port" in data and not 1 <= data["port"] <= 65535:
        raise ValueError("gscore port 必须位于 1 到 65535")
    if "token" in data and data["token"] != data["token"].strip():
        raise ValueError("gscore token 不能包含首尾空白")


REG = get_manager().register("gscore", DEFAULTS, clean_extra=True, validator=validate_config)


def get_config() -> dict[str, Any]:
    """读取合并后的 gscore 配置。"""
    return REG.load()
