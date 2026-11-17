"""表情包插件配置。"""

from __future__ import annotations

from typing import Any

from ...config import get_manager
from .defaults import DEFAULTS


def validate_config(data: dict[str, Any]) -> None:
    """校验表情包配置的类型和取值范围，不隐式转换字符串布尔值。"""
    if not isinstance(data, dict):
        raise ValueError("表情包配置必须是对象")
    for key, value in data.items():
        if key not in DEFAULTS:
            raise ValueError(f"表情包配置存在未知设置: {key}")
        default = DEFAULTS[key]
        if key == "command_prefixes":
            if value is not None and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
                raise ValueError("command_prefixes 必须是字符串数组或 null")
        elif key == "notice_prob":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError("notice_prob 必须是 0 到 1 之间的数字")
        elif isinstance(default, list):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{key} 必须是字符串数组")
        elif type(value) is not type(default):
            raise ValueError(f"表情包配置类型错误: {key}")
    if not str(data.get("base_url", DEFAULTS["base_url"])).strip():
        raise ValueError("base_url 不能为空")


REG = get_manager().register("memes", DEFAULTS, clean_extra=True, validator=validate_config)


def get_config() -> dict[str, Any]:
    """获取表情包完整配置。"""
    return REG.load()


def cfg_base_url() -> str:
    """获取表情生成服务地址。"""
    return str(get_config().get("base_url") or "http://127.0.0.1:2233")


def cfg_command_prefixes() -> list[str] | None:
    """获取命令前缀配置。"""
    prefixes = get_config().get("command_prefixes")
    if prefixes is None:
        return None
    return [str(prefix) for prefix in prefixes]


def cfg_disabled_list() -> list[str]:
    """获取全局禁用表情列表。"""
    return [str(item) for item in (get_config().get("disabled_list") or [])]


def cfg_check_resources_on_startup() -> bool:
    """是否在启动时检查表情资源。"""
    return bool(get_config().get("check_resources_on_startup", True))


def cfg_random_meme_show_info() -> bool:
    """随机表情时是否附带提示信息。"""
    return bool(get_config().get("random_meme_show_info", True))


def cfg_notice_prob() -> float:
    """随机表情提示概率。"""
    return float(get_config().get("notice_prob", 0.1) or 0.0)


def cfg_use_gif() -> bool:
    """是否启用 GIF 输出。"""
    return bool(get_config().get("use_gif", False))


def cfg_use_ban_word() -> bool:
    """是否启用屏蔽词。"""
    return bool(get_config().get("use_ban_word", True))


def cfg_whitelist_ids() -> list[str]:
    """获取保护白名单用户 QQ 号列表。"""
    return [str(user_id) for user_id in (get_config().get("whitelist_ids") or [])]


def cfg_protected_memes() -> list[str]:
    """获取需要保护的表情 key 列表。"""
    return [str(meme_key) for meme_key in (get_config().get("protected_memes") or [])]


def save_protection_config(whitelist_ids: list[str], protected_memes: list[str]) -> None:
    """保存表情保护配置。"""
    config = get_config()
    config["whitelist_ids"] = list(whitelist_ids)
    config["protected_memes"] = list(protected_memes)
    REG.save(config)
