"""独立会员链路的门禁、恢复命令和事务测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import OperationalError

from fxbot.core import SessionTarget
from fxbot.config import SYSTEM_DEFAULTS, ConfigManager
from fxbot.utils.cache import cache
from fxbot.core.scheduler import Scheduler
from fxbot.db.base_models import init_database, close_database
from fxbot.membership.gate import MembershipGate
from fxbot.core.application import Application
from fxbot.membership.guard import MembershipGuard
from fxbot.membership.tasks import MembershipMaintenance
from fxbot.runtime.settings import Settings
from fxbot.membership.service import MembershipError, membership_service
from fxbot.platforms.onebot11 import OneBot11Client, parse_event
from fxbot.membership.commands import MembershipCommands
from tests.migration.test_onebot11 import payload


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    """全部数据来自测试隔离目录，不使用旧框架对象。"""

    async def asyncSetUp(self) -> None:
        self.config = ConfigManager()
        config = deepcopy(SYSTEM_DEFAULTS)
        config["membership"]["enabled"] = True
        config["membership"]["batch_delay_seconds"] = 0
        self.enterContext(patch.object(self.config, "get_system", return_value=config))
        await init_database()
        self.addAsyncCleanup(close_database)
        cache.clear_prefix(("membership",))
        self.addCleanup(cache.clear_prefix, ("membership",))
        self.group_id = str(int(uuid4().hex[:8], 16) + 1)
        self.application = Application()
        await self.application.start()
        self.addAsyncCleanup(self.application.stop)
        self.sender = AsyncMock(return_value={})
        self.client = OneBot11Client("100", self.sender)
        self.guard = MembershipGuard(self.config, membership_service)
        self.gate = MembershipGate(self.config, self.guard)
        self.application.router.add_preprocessor(self.gate)
        scheduler = Scheduler(self.application)
        maintenance = MembershipMaintenance(self.application, self.config, membership_service, self.guard, scheduler)
        commands = MembershipCommands(
            self.application,
            self.config,
            membership_service,
            self.guard,
            Settings("test"),
            maintenance,
        )
        commands.install()
        self.key = SessionTarget("onebot11", "100", "group", self.group_id).key()
        self.addAsyncCleanup(membership_service.delete_group, self.key)

    def event(self, text: str):
        """构造当前测试群的协议事件。"""
        data = payload()
        data["group_id"] = int(self.group_id)
        data["message"] = [{"type": "text", "data": {"text": text}}]
        return parse_event(data, bot_id="100")

    async def test_unknown_group_is_denied_but_expiry_query_is_reachable(self) -> None:
        ordinary = await self.application.router.dispatch(self.client, self.event("/ping"))
        self.assertTrue(ordinary.rejected)
        query = await self.application.router.dispatch(self.client, self.event("ww到期"))
        self.assertEqual(query.handled, ("membership:expiry",))

    async def test_renewal_commits_and_invalidates_previous_denial(self) -> None:
        self.assertFalse(await self.gate(self.event("/ping")))
        code = await membership_service.generate_code(duration_value=1, duration_unit="day")
        result = await self.application.router.dispatch(self.client, self.event(f"ww续费{code.code}"))
        self.assertFalse(result.failed)
        self.assertTrue(await self.gate(self.event("/ping")))
        record = await membership_service.get_group(self.key)
        self.assertEqual(record.managed_by_bot, "onebot11:100")
        with self.assertRaises(MembershipError):
            await membership_service.redeem_code(code.code, self.key)

    async def test_database_failure_denies_business_and_does_not_query_for_recovery(self) -> None:
        error = OperationalError("query", {}, RuntimeError("unavailable"))
        with patch.object(membership_service, "get_group", side_effect=error):
            self.assertFalse(await self.gate(self.event("/ping")))
        with patch.object(self.guard, "check_membership_detail", new_callable=AsyncMock) as check:
            self.assertTrue(await self.gate(self.event("ww到期")))
        check.assert_not_awaited()

    async def test_group_cannot_invoke_owner_only_code_generation(self) -> None:
        self.config.get_system()["membership"]["enabled"] = False
        result = await self.application.router.dispatch(self.client, self.event("ww生成续费码1天"))
        self.assertEqual(result.denied, ("membership:generate",))
        self.sender.assert_not_awaited()

    async def test_other_platform_does_not_reuse_group_membership(self) -> None:
        event = self.event("/ping")
        with patch.object(self.guard, "check_membership_detail", return_value=SimpleNamespace(allowed=False)) as check:
            self.assertFalse(await self.gate(event))
        self.assertEqual(check.call_args.args[0], self.key)

    async def test_cross_platform_actor_and_bare_group_id_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.guard.check_membership_detail(self.key, "onebot12.qq:100:private:200")
        with self.assertRaises(ValueError):
            await self.guard.check_membership_detail(self.group_id, None)

    async def test_admin_exemption_is_recomputed_before_cached_denial(self) -> None:
        self.assertFalse(await self.gate(self.event("/ping")))
        self.config.get_system()["system"]["superusers"] = ["onebot11:100:private:200"]
        self.assertTrue(await self.gate(self.event("/ping")))
