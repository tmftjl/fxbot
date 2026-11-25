"""Bot 掉线监控：快照对比、宽限、重复提醒与超管投递。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fxbot.core import SessionTarget
from fxbot.core.errors import ActionFailed
from fxbot.plugins.bot_monitor import monitor

CONFIG = {
    "enabled": True,
    "interval_seconds": 30,
    "offline_grace_seconds": 60,
    "repeat_minutes": 30,
    "notify_recovery": True,
}


def bot(platform: str, bot_id: str, *, send=None) -> SimpleNamespace:
    return SimpleNamespace(platform=platform, bot_id=bot_id, send=send or AsyncMock())


def services(bots, superusers) -> SimpleNamespace:
    system = {"superusers": superusers}
    return SimpleNamespace(
        application=SimpleNamespace(bots=tuple(bots)),
        configuration=SimpleNamespace(get_system=lambda: {"system": system}),
    )


class BotMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.onebot = bot("onebot11", "100")
        self.onebot2 = bot("onebot11", "101")
        self.qq = bot("qq", "102445077")

    def run_tick(self, runtime, state, now):
        with patch.object(monitor, "get_config", return_value=CONFIG):
            return monitor.run_tick(runtime, state, now=now)

    async def test_first_tick_only_builds_baseline(self) -> None:
        state = {}
        await self.run_tick(services([self.onebot], ["200"]), state, 100)
        self.onebot.send.assert_not_awaited()
        self.assertTrue(state["initialized"])
        self.assertEqual(state["online"], {"onebot11:100"})

    async def test_alert_after_grace_and_respect_repeat(self) -> None:
        runtime = services([self.onebot, self.onebot2], ["200"])
        state = {}
        await self.run_tick(runtime, state, 100)
        runtime.application.bots = (self.onebot,)
        await self.run_tick(runtime, state, 130)
        self.onebot.send.assert_not_awaited()
        await self.run_tick(runtime, state, 191)
        self.onebot.send.assert_awaited_once()
        target, message = self.onebot.send.await_args.args
        self.assertEqual(target, SessionTarget("onebot11", "100", "private", "200"))
        self.assertIn("Bot 掉线", message.plain_text())
        self.assertIn("onebot11:101", message.plain_text())
        await self.run_tick(runtime, state, 1000)
        self.onebot.send.assert_awaited_once()
        await self.run_tick(runtime, state, 2000)
        self.onebot.send.assert_awaited()
        self.assertEqual(self.onebot.send.await_count, 2)

    async def test_recovery_clears_offline_state(self) -> None:
        runtime = services([self.onebot, self.onebot2], ["200"])
        state = {}
        await self.run_tick(runtime, state, 100)
        runtime.application.bots = (self.onebot,)
        await self.run_tick(runtime, state, 200)
        await self.run_tick(runtime, state, 261)  # 越过宽限，发出掉线提醒
        self.onebot.send.reset_mock()
        runtime.application.bots = (self.onebot, self.onebot2)
        await self.run_tick(runtime, state, 300)
        self.assertEqual(self.onebot.send.await_count, 1)
        self.assertIn("Bot 恢复", self.onebot.send.await_args.args[1].plain_text())
        self.assertEqual(state["offline"], {})

    async def test_short_flap_stays_silent(self) -> None:
        runtime = services([self.onebot, self.onebot2], ["200"])
        state = {}
        await self.run_tick(runtime, state, 100)
        runtime.application.bots = (self.onebot,)
        await self.run_tick(runtime, state, 130)
        runtime.application.bots = (self.onebot, self.onebot2)
        await self.run_tick(runtime, state, 160)
        self.onebot.send.assert_not_awaited()
        self.assertEqual(state["offline"], {})

    def test_duration_uses_seconds_below_a_minute(self) -> None:
        self.assertEqual(monitor._duration(3.2), "3 秒")
        self.assertEqual(monitor._duration(59.9), "59 秒")
        self.assertEqual(monitor._duration(60), "1 分钟")
        self.assertEqual(monitor._duration(3599), "59 分钟")
        self.assertEqual(monitor._duration(3661), "1 小时 1 分钟")

    async def test_openid_superuser_uses_qq_bot(self) -> None:
        dropped_qq = bot("qq", "1906397532")
        runtime = services([self.onebot, self.qq, dropped_qq], ["12EC7127C305E48A43D664129E856A8B"])
        state = {}
        await self.run_tick(runtime, state, 100)
        runtime.application.bots = (self.onebot, self.qq)
        await self.run_tick(runtime, state, 130)
        await self.run_tick(runtime, state, 191)
        self.qq.send.assert_awaited_once()
        self.onebot.send.assert_not_awaited()
        self.assertEqual(
            self.qq.send.await_args.args[0],
            SessionTarget("qq", "102445077", "private", "12EC7127C305E48A43D664129E856A8B"),
        )

    async def test_failed_delivery_is_retried_on_next_tick(self) -> None:
        failing = bot("onebot11", "100", send=AsyncMock(side_effect=ActionFailed("no")))
        dropped = bot("onebot11", "101")
        healthy = bot("onebot11", "102")
        runtime = services([failing, dropped], ["200"])
        state = {}
        await self.run_tick(runtime, state, 100)
        runtime.application.bots = (failing,)
        await self.run_tick(runtime, state, 130)
        await self.run_tick(runtime, state, 191)
        self.assertEqual(state["offline"]["onebot11:101"]["alerted"], 0.0)
        runtime.application.bots = (failing, healthy)
        await self.run_tick(runtime, state, 192)
        healthy.send.assert_awaited_once()
        self.assertIn("onebot11:101", healthy.send.await_args.args[1].plain_text())

    async def test_disabled_config_does_not_track(self) -> None:
        state = {}
        with patch.object(monitor, "get_config", return_value={**CONFIG, "enabled": False}):
            await monitor.run_tick(services([self.onebot], ["200"]), state, now=100)
        self.onebot.send.assert_not_awaited()
        self.assertEqual(state, {})
