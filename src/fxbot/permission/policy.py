"""权限策略链。"""

from __future__ import annotations

import abc
from typing import Any

from .types import Decision, PermLevel, PermScene, PermContext, PolicyResult


class PermissionPolicy(abc.ABC):
    """权限策略基类。"""

    @abc.abstractmethod
    async def evaluate(self, config: dict[str, Any], context: PermContext) -> PolicyResult:
        """评估策略。"""


class EnabledPolicy(PermissionPolicy):
    """功能开关策略。"""

    async def evaluate(self, config: dict[str, Any], context: PermContext) -> PolicyResult:
        if not config.get("enabled", True):
            return PolicyResult.deny("该功能已禁用")
        return PolicyResult.allow()


class BlacklistPolicy(PermissionPolicy):
    """黑名单策略。"""

    async def evaluate(self, config: dict[str, Any], context: PermContext) -> PolicyResult:
        blacklist = config.get("blacklist") if isinstance(config.get("blacklist"), dict) else {}
        if context.user_id in blacklist.get("users", []):
            return PolicyResult.deny("用户在黑名单中")
        if context.group_id and context.group_id in blacklist.get("groups", []):
            return PolicyResult.deny("群组在黑名单中")
        return PolicyResult.allow()


class WhitelistPolicy(PermissionPolicy):
    """白名单策略。"""

    async def evaluate(self, config: dict[str, Any], context: PermContext) -> PolicyResult:
        whitelist = config.get("whitelist") if isinstance(config.get("whitelist"), dict) else {}
        users = whitelist.get("users", [])
        groups = whitelist.get("groups", [])
        if not users and not groups:
            return PolicyResult.skip()
        if context.user_id in users:
            return PolicyResult.allow("用户在白名单中")
        if context.group_id and context.group_id in groups:
            return PolicyResult.allow("群组在白名单中")
        if not context.group_id and not users:
            return PolicyResult.skip()
        return PolicyResult.deny("不在白名单中")


class ScenePolicy(PermissionPolicy):
    """群聊/私聊场景策略。"""

    async def evaluate(self, config: dict[str, Any], context: PermContext) -> PolicyResult:
        scene = PermScene.from_str(config.get("scene", "all"))
        if scene == PermScene.ALL:
            return PolicyResult.allow()
        if scene == PermScene.GROUP and context.is_group:
            return PolicyResult.allow()
        if scene == PermScene.PRIVATE and context.is_private:
            return PolicyResult.allow()
        return PolicyResult.deny(f"不支持的场景: {scene.value}")


class LevelPolicy(PermissionPolicy):
    """权限等级策略。"""

    async def evaluate(self, config: dict[str, Any], context: PermContext) -> PolicyResult:
        required_level = PermLevel.from_str(config.get("level", "member"))
        if context.is_private and required_level in {PermLevel.ADMIN, PermLevel.OWNER}:
            required_level = PermLevel.LOW
        if context.user_level >= required_level:
            return PolicyResult.allow()
        return PolicyResult.deny(f"需要 {required_level.name} 及以上权限")


class PolicyChain:
    """按固定顺序执行权限策略。"""

    def __init__(self, policies: list[PermissionPolicy] | None = None) -> None:
        self._policies = policies or [
            EnabledPolicy(),
            BlacklistPolicy(),
            WhitelistPolicy(),
            ScenePolicy(),
            LevelPolicy(),
        ]

    async def evaluate(self, config: dict[str, Any], context: PermContext) -> PolicyResult:
        """按顺序执行策略并返回最终结果。"""
        for policy in self._policies:
            result = await policy.evaluate(config, context)
            if result.decision == Decision.DENY:
                return result
            if result.decision == Decision.ALLOW and isinstance(policy, WhitelistPolicy):
                return result
        return PolicyResult.allow()


class FeaturePolicy:
    """统一评估全局、插件和命令三层策略，不读取事件或宿主。"""

    def __init__(self) -> None:
        """共享一条普通策略链及主人豁免前的黑名单检查。"""
        self._chain = PolicyChain()
        self._blacklist = BlacklistPolicy()

    @staticmethod
    def entries(config: dict[str, Any], feature: str) -> list[dict[str, Any]]:
        """明确取出当前功能实际受影响的策略层。"""
        plugin, separator, command = feature.partition(":")
        plugins = config.get("sub_plugins", {})
        node = plugins.get(plugin, {})
        layers = [config.get("top"), node.get("top"), node.get("commands", {}).get(command) if separator else None]
        return [entry for entry in layers if entry is not None]

    async def evaluate(self, config: dict[str, Any], feature: str, context: PermContext) -> bool:
        """开关和黑名单优先于主人，普通用户必须通过全部策略层。"""
        entries = self.entries(config, feature)
        if any(not entry.get("enabled", True) for entry in entries):
            return False
        for entry in entries:
            if (await self._blacklist.evaluate(entry, context)).decision is Decision.DENY:
                return False
        if context.user_level is PermLevel.SUPERUSER:
            return True
        for entry in entries:
            if (await self._chain.evaluate(entry, context)).decision is Decision.DENY:
                return False
        return True
