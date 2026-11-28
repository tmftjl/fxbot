"""Bot 监控：掉线与假在线共用判据、阈值处置、节流与单条提醒。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fxbot.plugins.bot_monitor import liveness, monitor


def bot(platform: str, bot_id: str, *, call=None) -> SimpleNamespace:
    return SimpleNamespace(
        platform=platform,
        bot_id=bot_id,
        send=AsyncMock(),
        call=call or AsyncMock(),
    )


def failing() -> AsyncMock:
    return AsyncMock(side_effect=RuntimeError("平台动作失败"))


def services(bots, superusers=("200",)) -> SimpleNamespace:
    system = {"superusers": list(superusers)}
    return SimpleNamespace(
        application=SimpleNamespace(bots=tuple(bots)),
        configuration=SimpleNamespace(get_system=lambda: {"system": system}),
    )


class BotMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.restarted: list[str] = []

        async def fake_run(*args: str) -> tuple[int, str]:
            if args[:2] == ("docker", "ps"):
                return 0, "napcat2\n"
            if args[:2] == ("docker", "exec"):
                return 0, "onebot11_100.json\nnapcat.json\n"
            if args[:2] == ("docker", "restart"):
                self.restarted.append(args[2])
                return 0, ""
            return 1, ""

        patcher = patch.object(liveness, "_run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def tick(self, runtime, state, now):
        await monitor.run_tick(runtime, state, now=now)

    def texts(self, *bots) -> list[str]:
        return [item.args[1].plain_text() for item in [call for b in bots for call in b.send.await_args_list]]

    async def test_first_tick_establishes_baseline(self) -> None:
        state: dict = {}
        runtime = services([bot("onebot11", "100")])
        await self.tick(runtime, state, 0)
        self.assertEqual(self.restarted, [])
        self.assertEqual(state["fails"], {"onebot11:100": 0})
        self.assertEqual(state["known"], {"onebot11:100"})

    async def test_offline_three_ticks_restarts_container(self) -> None:
        state: dict = {}
        online = services([bot("onebot11", "100")])
        await self.tick(online, state, 0)
        missing = services([])
        await self.tick(missing, state, 300)
        await self.tick(missing, state, 600)
        self.assertEqual(self.restarted, [])
        await self.tick(missing, state, 900)
        self.assertEqual(self.restarted, ["napcat2"])

    async def test_probe_failure_counts_like_offline(self) -> None:
        state: dict = {}
        runtime = services([bot("onebot11", "100", call=failing())])
        await self.tick(runtime, state, 0)
        await self.tick(runtime, state, 300)
        self.assertEqual(self.restarted, [])
        await self.tick(runtime, state, 600)
        self.assertEqual(self.restarted, ["napcat2"])
        self.assertEqual(state["fails"]["onebot11:100"], 0)

    async def test_recovery_between_ticks_resets_counter(self) -> None:
        state: dict = {}
        online = services([bot("onebot11", "100")])
        missing = services([])
        await self.tick(online, state, 0)
        await self.tick(missing, state, 300)
        self.assertEqual(state["fails"]["onebot11:100"], 1)
        await self.tick(online, state, 600)
        self.assertEqual(state["fails"]["onebot11:100"], 0)
        await self.tick(missing, state, 900)
        self.assertEqual(state["fails"]["onebot11:100"], 1)
        self.assertEqual(self.restarted, [])

    async def test_qq_platform_is_not_probed(self) -> None:
        state: dict = {}
        qq = bot("qq", "102445077", call=failing())
        runtime = services([qq])
        await self.tick(runtime, state, 0)
        qq.call.assert_not_awaited()
        self.assertEqual(state["fails"]["qq:102445077"], 0)

    async def test_unknown_container_alerts_without_restart(self) -> None:
        state: dict = {}
        runtime = services([bot("onebot11", "999", call=failing())])
        await self.tick(runtime, state, 0)
        await self.tick(runtime, state, 300)
        await self.tick(runtime, state, 600)
        self.assertEqual(self.restarted, [])
        self.assertIn("未找到对应容器", self.texts(*runtime.application.bots)[-1])

    async def test_cooldown_blocks_second_action(self) -> None:
        state: dict = {}
        runtime = services([bot("onebot11", "100", call=failing())])
        for now in (0, 300, 600):
            await self.tick(runtime, state, now)
        self.assertEqual(self.restarted, ["napcat2"])
        for now in (700, 800, 900):
            await self.tick(runtime, state, now)
        self.assertEqual(self.restarted, ["napcat2"])
        for now in (1000, 1100, 1500):
            await self.tick(runtime, state, now)
        self.assertEqual(self.restarted, ["napcat2", "napcat2"])

    async def test_single_notification_per_action(self) -> None:
        state: dict = {}
        first = bot("onebot11", "100", call=failing())
        second = bot("onebot11", "101")
        runtime = services([first, second], superusers=("200", "201"))
        for now in (0, 300, 600):
            await self.tick(runtime, state, now)
        self.assertEqual(first.send.await_count + second.send.await_count, 1)
        self.assertIn("Bot 异常", self.texts(first, second)[0])

    async def test_recovery_notification_follows_alert(self) -> None:
        state: dict = {}
        broken = services([bot("onebot11", "100", call=failing())])
        for now in (0, 300, 600):
            await self.tick(broken, state, now)
        self.assertIn("Bot 异常", self.texts(*broken.application.bots)[-1])
        healthy = services([bot("onebot11", "100")])
        await self.tick(healthy, state, 900)
        self.assertIn("Bot 恢复", self.texts(*healthy.application.bots)[-1])
        self.assertEqual(state["alerted"], set())

    async def test_discovery_maps_account_to_container(self) -> None:
        state: dict = {}
        mapping = await liveness.discover_containers(state, 0.0)
        self.assertEqual(mapping, {"100": "napcat2"})
        refreshed = await liveness.discover_containers(state, 1.0)
        self.assertEqual(refreshed, mapping)

    def test_duration_uses_seconds_below_a_minute(self) -> None:
        self.assertEqual(monitor._duration(3.2), "3 秒")
        self.assertEqual(monitor._duration(59.9), "59 秒")
        self.assertEqual(monitor._duration(60), "1 分钟")
        self.assertEqual(monitor._duration(3599), "59 分钟")
        self.assertEqual(monitor._duration(3661), "1 小时 1 分钟")
