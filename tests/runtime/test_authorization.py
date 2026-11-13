"""原生权限服务的身份隔离和策略优先级测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import Mock

from fxbot.core import SessionTarget
from fxbot.config import SYSTEM_DEFAULTS, ConfigManager
from fxbot.permission.types import PermLevel, perm_entry_default
from fxbot.platforms.onebot11 import parse_event
from tests.migration.test_onebot11 import payload
from fxbot.permission.authorization import Authorizer


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    """输入只有核心事件，不创建框架 Bot 或 Permission。"""

    async def test_background_target_does_not_inherit_superuser_exemption(self) -> None:
        self.system["system"]["superusers"] = [self.identity]
        self.permissions["top"]["level"] = "superuser"
        self.assertTrue(await self.authorizer.check("feature", self.event))
        self.assertFalse(await self.authorizer.check_target("feature", self.event.target))

    async def test_background_target_uses_current_scoped_blacklist(self) -> None:
        target = self.event.target
        self.assertTrue(await self.authorizer.check_target("feature", target))
        self.permissions["top"]["blacklist"]["groups"] = [target.key()]
        self.assertFalse(await self.authorizer.check_target("feature", target))
        other = SessionTarget(target.platform, "101", target.kind, target.target_id)
        self.assertTrue(await self.authorizer.check_target("feature", other))

    def setUp(self) -> None:
        self.system = deepcopy(SYSTEM_DEFAULTS)
        self.permissions = {
            "top": perm_entry_default(),
            "sub_plugins": {"feature": {"top": perm_entry_default(), "commands": {"run": perm_entry_default()}}},
        }
        self.configuration = Mock(spec=ConfigManager)
        self.configuration.get_system.return_value = self.system
        self.storage = Mock()
        self.storage.load.return_value = self.permissions
        self.authorizer = Authorizer(self.configuration, self.storage)
        self.event = parse_event(payload(), bot_id="100")
        self.identity = "onebot11:100:private:200"

    async def test_owner_does_not_bypass_disabled_feature_or_blacklist(self) -> None:
        self.system["system"]["superusers"] = [self.identity]
        self.permissions["top"]["enabled"] = False
        self.assertFalse(await self.authorizer.check("feature:run", self.event))
        self.permissions["top"]["enabled"] = True
        command = self.permissions["sub_plugins"]["feature"]["commands"]["run"]
        command["blacklist"]["users"] = [self.identity]
        self.assertFalse(await self.authorizer.check("feature:run", self.event))

    async def test_owner_exemption_is_recomputed_after_configuration_change(self) -> None:
        self.permissions["top"]["level"] = "superuser"
        self.assertFalse(await self.authorizer.check("feature:run", self.event))
        self.system["system"]["superusers"] = [self.identity]
        self.assertTrue(await self.authorizer.check("feature:run", self.event))
        self.system["system"]["superusers"] = []
        self.system["system"]["bot_admins"] = [self.identity]
        self.assertFalse(await self.authorizer.check("feature:run", self.event))

    async def test_raw_id_does_not_grant_cross_platform_owner_identity(self) -> None:
        self.system["system"]["superusers"] = ["200", "onebot12.qq:100:private:200"]
        self.assertNotEqual(self.authorizer.context(self.event, self.permissions).user_level, PermLevel.SUPERUSER)

    async def test_sender_role_is_read_only_from_normalized_metadata(self) -> None:
        self.permissions["top"]["level"] = "admin"
        self.assertTrue(await self.authorizer.check("feature:run", self.event))
        self.event.metadata["sender_role"] = None
        self.assertFalse(await self.authorizer.check("feature:run", self.event))

    async def test_whitelist_requires_full_group_or_user_identity(self) -> None:
        self.permissions["top"]["whitelist"]["groups"] = ["300"]
        with self.assertRaises(ValueError):
            await self.authorizer.check("feature:run", self.event)
        self.permissions["top"]["whitelist"]["groups"] = [self.event.target.key()]
        self.assertTrue(await self.authorizer.check("feature:run", self.event))

    async def test_unmigrated_blacklist_does_not_silently_stop_matching(self) -> None:
        self.permissions["top"]["blacklist"]["users"] = ["200"]
        with self.assertRaises(ValueError):
            await self.authorizer.check("feature:run", self.event)

    async def test_scene_is_checked_for_non_owner(self) -> None:
        self.permissions["top"]["scene"] = "private"
        self.assertFalse(await self.authorizer.guard("feature:run")(self.event))

    async def test_bad_permissions_do_not_turn_into_public_access(self) -> None:
        self.storage.load.return_value = {}
        with self.assertRaises(ValueError):
            await self.authorizer.check("feature:run", self.event)

    async def test_missing_command_or_plugin_is_denied_not_defaulted_to_global_access(self) -> None:
        self.assertFalse(await self.authorizer.check("unknown", self.event))
        self.permissions["sub_plugins"]["feature"]["commands"].clear()
        self.assertFalse(await self.authorizer.check("feature:run", self.event))
        self.assertFalse(await self.authorizer.check_target("feature:run", self.event.target))

    async def test_invalid_feature_is_registration_error(self) -> None:
        for value in ("", ":run", "plugin:", "a:b:c"):
            with self.assertRaises(ValueError):
                self.authorizer.guard(value)
