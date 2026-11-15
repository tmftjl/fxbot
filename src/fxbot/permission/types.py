"""权限枚举和上下文类型。"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any
from dataclasses import dataclass


class PermLevel(IntEnum):
    """权限等级。"""

    LOW = 0
    MEMBER = 1
    ADMIN = 2
    OWNER = 3
    BOT_ADMIN = 4
    SUPERUSER = 5

    @staticmethod
    def from_str(value: str) -> "PermLevel":
        """从字符串转换权限等级。"""
        mapping = {
            "all": PermLevel.LOW,
            "low": PermLevel.LOW,
            "member": PermLevel.MEMBER,
            "admin": PermLevel.ADMIN,
            "owner": PermLevel.OWNER,
            "bot_admin": PermLevel.BOT_ADMIN,
            "superuser": PermLevel.SUPERUSER,
        }
        if not isinstance(value, str) or value not in mapping:
            raise ValueError(f"未知权限等级: {value!r}")
        return mapping[value]


class PermScene(Enum):
    """权限适用场景。"""

    ALL = "all"
    GROUP = "group"
    PRIVATE = "private"

    @staticmethod
    def from_str(value: str) -> "PermScene":
        """从字符串转换权限场景。"""
        mapping = {
            "all": PermScene.ALL,
            "group": PermScene.GROUP,
            "private": PermScene.PRIVATE,
        }
        if not isinstance(value, str) or value not in mapping:
            raise ValueError(f"未知权限场景: {value!r}")
        return mapping[value]


@dataclass
class PermContext:
    """权限判定上下文。"""

    user_id: str
    group_id: str | None
    user_level: PermLevel
    is_group: bool
    is_private: bool


def perm_entry_default(level: str = "member", scene: str = "all") -> dict[str, Any]:
    """生成默认权限条目。"""
    return {
        "enabled": True,
        "level": level,
        "scene": scene,
        "whitelist": {"users": [], "groups": []},
        "blacklist": {"users": [], "groups": []},
    }
