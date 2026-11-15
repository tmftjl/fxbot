"""娱乐插件配置。"""

from __future__ import annotations

from typing import Any

from ...config import get_manager
from .defaults import DEFAULTS
from ...core.types import SessionTarget


def validate_config(data: dict[str, Any]) -> None:
    for section, values in data.items():
        if section not in DEFAULTS or not isinstance(values, dict):
            raise ValueError("娱乐配置需要已知分区")
        for key, value in values.items():
            if key not in DEFAULTS[section] or type(value) is not type(DEFAULTS[section][key]):
                raise ValueError(f"娱乐配置类型错误: {section}.{key}")
    music = data.get("music", {})
    for key, maximum in (("search_num", 50), ("qq_quality", 4), ("netease_quality", 8)):
        if key in music and not 1 <= music[key] <= maximum:
            raise ValueError(f"音乐配置超出范围: {key}")
    if "login_mode" in music and music["login_mode"] not in {"shared", "per_user"}:
        raise ValueError("登录模式必须为 shared 或 per_user")
    if "provider_default" in music and music["provider_default"] not in {"tencent", "netease"}:
        raise ValueError("默认音乐平台错误")
    box = data.get("box", {})
    for field, kind in (("box_blacklist", "private"), ("auto_box_groups", "group")):
        for key in box.get(field, []):
            if not isinstance(key, str) or SessionTarget.from_key(key).kind != kind:
                raise ValueError(f"{field} 必须使用完整会话键")
    display = data.get("image_display", {})
    if "type" in display and display["type"] not in {1, 2, 3}:
        raise ValueError("图片外显类型必须为 1、2、3")
    if "list" in display and (
        not display["list"] or not all(isinstance(item, str) and item for item in display["list"])
    ):
        raise ValueError("图片外显列表必须含非空字符串")


REG = get_manager().register("entertain", DEFAULTS, clean_extra=True, validator=validate_config)


def get_config() -> dict[str, Any]:
    """获取娱乐插件完整配置。"""
    return REG.load()


def save_config(cfg: dict[str, Any]) -> None:
    """保存配置。"""
    REG.save(cfg)


def cfg_api_urls() -> dict[str, Any]:
    """获取娱乐插件 API 地址配置。"""
    return get_config()["api_urls"]


def cfg_music() -> dict[str, Any]:
    """获取点歌配置。"""
    return get_config()["music"]


def cfg_box() -> dict[str, Any]:
    """获取开盒配置。"""
    return get_config()["box"]


def cfg_image_display() -> dict[str, Any]:
    """获取图片外显配置。"""
    return get_config()["image_display"]
