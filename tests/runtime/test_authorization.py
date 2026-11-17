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

    async def test_background_target_blacklist_uses_bare_group_id(self) -> None:
        target = self.event.target
        self.assertTrue(await self.authorizer.check_target("feature", target))
        self.permissions["top"]["blacklist"]["groups"] = [target.target_id]
        self.assertFalse(await self.authorizer.check_target("feature", target))
        other_bot = SessionTarget(target.platform, "101", target.kind, target.target_id)
        self.assertFalse(await self.authorizer.check_target("feature", other_bot))
        other_group = SessionTarget(target.platform, "101", target.kind, "301")
        self.assertTrue(await self.authorizer.check_target("feature", other_group))

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
        command["blacklist"]["users"] = ["200"]
        self.assertFalse(await self.authorizer.check("feature:run", self.event))

    async def test_owner_exemption_is_recomputed_after_configuration_change(self) -> None:
        self.permissions["top"]["level"] = "superuser"
        self.assertFalse(await self.authorizer.check("feature:run", self.event))
        self.system["system"]["superusers"] = [self.identity]
        self.assertTrue(await self.authorizer.check("feature:run", self.event))
        self.system["system"]["superusers"] = []
        self.system["system"]["bot_admins"] = ["200"]
        self.assertFalse(await self.authorizer.check("feature:run", self.event))

    async def test_raw_id_grants_superuser_for_nonebot_compatibility(self) -> None:
        self.system["system"]["superusers"] = ["200", "onebot12.qq:100:private:200"]
        self.assertEqual(self.authorizer.context(self.event, self.permissions).user_level, PermLevel.SUPERUSER)
        self.system["system"]["superusers"] = ["201"]
        self.assertNotEqual(self.authorizer.context(self.event, self.permissions).user_level, PermLevel.SUPERUSER)

    async def test_sender_role_is_read_only_from_normalized_metadata(self) -> None:
        self.permissions["top"]["level"] = "admin"
        self.assertTrue(await self.authorizer.check("feature:run", self.event))
        self.event.metadata["sender_role"] = None
        self.assertFalse(await self.authorizer.check("feature:run", self.event))

    async def test_whitelist_accepts_bare_group_id_and_rejects_full_key(self) -> None:
        self.permissions["top"]["whitelist"]["groups"] = ["300"]
        self.assertTrue(await self.authorizer.check("feature:run", self.event))
        self.permissions["top"]["whitelist"]["groups"] = [self.event.target.key()]
        with self.assertRaises(ValueError):
            await self.authorizer.check("feature:run", self.event)

    async def test_bare_blacklist_user_id_still_matches(self) -> None:
        self.permissions["top"]["blacklist"]["users"] = ["200"]
        self.assertFalse(await self.authorizer.check("feature:run", self.event))
        self.permissions["top"]["blacklist"]["users"] = ["201"]
        self.assertTrue(await self.authorizer.check("feature:run", self.event))

    async def test_bare_id_grants_bot_admin(self) -> None:
        self.system["system"]["bot_admins"] = ["200"]
        self.permissions["top"]["level"] = "bot_admin"
        self.assertTrue(await self.authorizer.check("feature:run", self.event))

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
