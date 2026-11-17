"""明确的权限阶段：强制拒绝、主人豁免、逐层普通规则。"""

from __future__ import annotations

from typing import Any

from ..core import SessionTarget
from .types import PermLevel, PermScene, PermContext


def _matches(values: object, identity: str) -> bool:
    """权限列表只存裸 ID，匹配时从上下文键中取出目标 ID 比较。"""
    if not isinstance(values, list):
        return False
    try:
        target_id = SessionTarget.from_key(identity).target_id
    except ValueError:
        return False
    return target_id in values


def _listed(scope: dict, context: PermContext) -> bool:
    return _matches(scope.get("users", []), context.user_id) or bool(
        context.group_id and _matches(scope.get("groups", []), context.group_id)
    )


def _allows(entry: dict, context: PermContext) -> bool:
    whitelist = entry.get("whitelist", {})
    users, groups = whitelist.get("users", []), whitelist.get("groups", [])
    if users or groups:
        if _listed(whitelist, context):
            return True
        if context.group_id or users:
            return False
    scene = PermScene.from_str(entry.get("scene", "all"))
    if scene is PermScene.GROUP and not context.is_group:
        return False
    if scene is PermScene.PRIVATE and not context.is_private:
        return False
    required = PermLevel.from_str(entry.get("level", "member"))
    if context.is_private and required in {PermLevel.ADMIN, PermLevel.OWNER}:
        required = PermLevel.LOW
    return context.user_level >= required


class FeaturePolicy:
    """全局、插件、命令都通过；白名单仅豁免所在层的普通限制。"""

    @staticmethod
    def entries(config: dict[str, Any], feature: str) -> list[dict[str, Any]]:
        plugin, separator, command = feature.partition(":")
        node = config.get("sub_plugins", {}).get(plugin, {})
        layers = [config.get("top"), node.get("top"), node.get("commands", {}).get(command) if separator else None]
        return [entry for entry in layers if entry is not None]

    async def evaluate(self, config: dict[str, Any], feature: str, context: PermContext) -> bool:
        entries = self.entries(config, feature)
        if any(not entry.get("enabled", True) or _listed(entry.get("blacklist", {}), context) for entry in entries):
            return False
        if context.user_level is PermLevel.SUPERUSER:
            return True
        return all(_allows(entry, context) for entry in entries)
