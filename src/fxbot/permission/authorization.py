"""直接消费核心事件的权限服务。"""

from __future__ import annotations

from ..core import Event, SessionTarget
from .types import PermLevel, PermContext
from .policy import FeaturePolicy
from ..config import ConfigManager
from .helpers import upsert_plugin_defaults, upsert_command_defaults
from .storage import PermissionStorage
from .validation import validate_entry
from ..core.router import Guard
from ..core.commands import Command


class Authorizer:
    """应用显式拥有配置、存储和领域策略，不依赖外部框架。"""

    def __init__(self, configuration: ConfigManager, storage: PermissionStorage) -> None:
        """绑定权限依赖，策略评估不缓存身份豁免。"""
        self.configuration = configuration
        self.storage = storage
        self.policy = FeaturePolicy()

    def context(self, event: Event, permissions: dict) -> PermContext:
        """用户与群身份均使用带平台和 Bot 的规范键。"""
        user = SessionTarget(event.platform, event.bot_id, "private", event.user_id).key()
        system = self.configuration.get_system()["system"]
        admins = set(system["bot_admins"]) | set(permissions.get("bot_admins", []))
        admins.update(permissions.get("top", {}).get("bot_admins", []))
        if user in system["superusers"]:
            level = PermLevel.SUPERUSER
        elif user in admins:
            level = PermLevel.BOT_ADMIN
        elif event.is_group and event.metadata.get("sender_role") == "owner":
            level = PermLevel.OWNER
        elif event.is_group and event.metadata.get("sender_role") == "admin":
            level = PermLevel.ADMIN
        else:
            level = PermLevel.MEMBER if event.target.kind in {"group", "private", "channel", "guild"} else PermLevel.LOW
        return PermContext(
            user_id=user,
            group_id=event.target.key() if event.is_group else None,
            user_level=level,
            is_group=event.is_group,
            is_private=event.target.kind == "private",
        )

    async def check(self, feature: str, event: Event) -> bool:
        """每次从有效配置计算权限，坏配置由分发边界拒绝。"""
        permissions = self._permissions(feature)
        if permissions is None:
            return False
        return await self.policy.evaluate(permissions, feature, self.context(event, permissions))

    def _permissions(self, feature: str) -> dict | None:
        """后台目标和真实用户事件使用相同的策略结构校验。"""
        permissions = self.storage.load()
        if not isinstance(permissions.get("top"), dict):
            raise ValueError("权限配置缺少全局策略")
        plugin, separator, command = feature.partition(":")
        node = permissions.get("sub_plugins", {}).get(plugin)
        if not isinstance(node, dict) or not isinstance(node.get("top"), dict):
            return None
        if separator and not isinstance(node.get("commands", {}).get(command), dict):
            return None
        for entry in self.policy.entries(permissions, feature):
            validate_entry(entry)
        return permissions

    async def check_target(self, feature: str, target: SessionTarget) -> bool:
        """后台通知只代表目标会话，不继承订阅操作者的管理员豁免。"""
        if target.kind not in {"group", "private"}:
            return False
        context = PermContext(
            user_id=target.key() if target.kind == "private" else "",
            group_id=target.key() if target.kind == "group" else None,
            user_level=PermLevel.MEMBER,
            is_group=target.kind == "group",
            is_private=target.kind == "private",
        )
        permissions = self._permissions(feature)
        return permissions is not None and await self.policy.evaluate(permissions, feature, context)

    def guard(self, feature: str) -> Guard:
        """构造原生路由权限函数，不创建 Matcher 或 Permission 对象。"""
        if not feature or feature.count(":") > 1 or any(not part for part in feature.split(":")):
            raise ValueError("功能名必须是插件名或插件名:命令名")

        async def allow(event: Event) -> bool:
            """路由只传递核心事件。"""
            return await self.check(feature, event)

        return allow

    def declare_commands(self, feature: str, commands: tuple[Command, ...]) -> None:
        """写入命令默认权限，保留已有用户配置。"""
        upsert_plugin_defaults(feature, level="all", scene="all", storage=self.storage)
        for command in commands:
            upsert_command_defaults(
                feature,
                command.name,
                level=command.level,
                scene=command.scene,
                storage=self.storage,
            )
        self.storage.flush()
