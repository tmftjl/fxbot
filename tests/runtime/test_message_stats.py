"""消息发送统计的旧版缓存格式、跨天归档与发送观察者测试。"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fxbot.core import Message, SessionTarget
from fxbot.core.application import Application
from fxbot.platforms.onebot11 import OneBot11Client
from fxbot.runtime.message_stats import MessageStats


def _today(value: str):
    """固定统计使用的北京日期。"""
    return patch("fxbot.runtime.message_stats.today_str", return_value=value)


class MessageStatsTests(unittest.IsolatedAsyncioTestCase):
    """累计、归档和磁盘读取都保持旧版字段与口径。"""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "system" / "status_message_cache.json"
        self.stats = MessageStats(self.path)
        self.clock_patch = _today("2026-11-18")
        self.clock = self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    @staticmethod
    def _bot(bot_id: str = "100"):
        return SimpleNamespace(platform="onebot11", bot_id=bot_id)

    @staticmethod
    def _target(kind: str = "group", target_id: str = "300"):
        return SessionTarget("onebot11", "100", kind, target_id)

    async def test_counts_group_and_private_but_ignores_channel(self) -> None:
        await self.stats.record_send(self._bot(), self._target("group", "300"))
        await self.stats.record_send(self._bot(), self._target("group", "300"))
        await self.stats.record_send(self._bot(), self._target("private", "200"))
        await self.stats.record_send(self._bot(), self._target("channel", "400"))

        self.assertEqual(
            await self.stats.today(),
            {
                "bots": {
                    "100": {
                        "total_sent": 3,
                        "group": {"count": 2, "targets": {"300": 2}},
                        "private": {"count": 1, "targets": {"200": 1}},
                    }
                }
            },
        )

    async def test_flush_writes_old_cache_shape_and_reloads(self) -> None:
        await self.stats.record_send(self._bot(), self._target())
        await self.stats.flush()

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["date"], "2026-11-18")
        self.assertEqual(raw["bots"]["bot_100"]["sent"], 1)
        self.assertEqual(raw["bots"]["bot_100"]["group"]["targets"], {"300": 1})
        self.assertEqual(raw["history"]["2026-11-18"], {"sent": 1, "group": 1, "private": 0})

        loaded = MessageStats(self.path)
        loaded.load()
        self.assertEqual(await loaded.today(), await self.stats.today())

    async def test_previous_day_is_archived_before_today_is_loaded(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "date": "2026-11-17",
                    "bots": {
                        "bot_100": {
                            "received": 4,
                            "sent": 3,
                            "group": {"count": 2, "targets": {"300": 2}},
                            "private": {"count": 1, "targets": {"200": 1}},
                        }
                    },
                    "history": {"2026-11-16": {"sent": 5, "group": 4, "private": 1}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        loaded = MessageStats(self.path)
        loaded.load()
        self.assertEqual(await loaded.today(), {"bots": {}})
        history = await loaded.history(days=3)
        self.assertEqual(
            history["items"],
            [
                {"date": "2026-11-16", "sent": 5, "group": 4, "private": 1},
                {"date": "2026-11-17", "sent": 3, "group": 2, "private": 1},
                {"date": "2026-11-18", "sent": 0, "group": 0, "private": 0},
            ],
        )

        await loaded.flush()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["bots"], {})
        self.assertEqual(raw["history"]["2026-11-17"]["sent"], 3)

    async def test_day_rollover_archives_sends_without_restart(self) -> None:
        self.clock.return_value = "2026-11-17"
        await self.stats.record_send(self._bot(), self._target())
        self.clock.return_value = "2026-11-18"
        await self.stats.record_send(self._bot(), self._target("private", "200"))

        self.assertEqual(
            await self.stats.today(),
            {"bots": {"100": {"total_sent": 1, "private": {"count": 1, "targets": {"200": 1}}}}},
        )
        history = await self.stats.history(days=2)
        self.assertEqual(
            history["items"],
            [
                {"date": "2026-11-17", "sent": 1, "group": 1, "private": 0},
                {"date": "2026-11-18", "sent": 1, "group": 0, "private": 1},
            ],
        )

    async def test_loaded_received_count_is_preserved_on_rewrite(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "date": "2026-11-18",
                    "bots": {"bot_100": {"received": 7, "sent": 1}},
                    "history": {},
                }
            ),
            encoding="utf-8",
        )
        loaded = MessageStats(self.path)
        loaded.load()
        await loaded.flush()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["bots"]["bot_100"]["received"], 7)


class SendObserverTests(unittest.IsolatedAsyncioTestCase):
    """应用只在发送成功后通知观察者，失败不影响原发送。"""

    async def asyncSetUp(self) -> None:
        self.app = Application()
        await self.app.start()
        self.addAsyncCleanup(self.app.stop)
        self.observer = AsyncMock()
        self.target = SessionTarget("test", "100", "group", "300")

    async def test_observer_runs_after_success_only(self) -> None:
        send = AsyncMock(return_value={"message_id": 1})
        bot = SimpleNamespace(platform="test", bot_id="100", send=send)
        self.app.add_send_observer(self.observer)
        self.app.register(bot, close=AsyncMock())

        self.assertEqual(await bot.send(self.target, Message.text("ok")), {"message_id": 1})
        send.assert_awaited_once()
        self.observer.assert_awaited_once_with(bot, self.target)

        send.side_effect = OSError("failed")
        with self.assertRaises(OSError):
            await bot.send(self.target, Message.text("fail"))
        self.observer.assert_awaited_once()

    async def test_observer_added_after_registration_and_forward_sends(self) -> None:
        send, forward = AsyncMock(), AsyncMock(return_value={"message_id": 2})
        bot = SimpleNamespace(platform="test", bot_id="100", send=send, send_forward=forward)
        self.app.register(bot, close=AsyncMock())
        self.app.add_send_observer(self.observer)

        await bot.send(self.target, Message.text("ok"))
        await bot.send_forward(self.target, [Message.text("ok")])
        self.assertEqual(self.observer.await_count, 2)

    async def test_observer_failure_does_not_break_send(self) -> None:
        self.observer.side_effect = RuntimeError("broken observer")
        bot = SimpleNamespace(platform="test", bot_id="100", send=AsyncMock(return_value="receipt"))
        self.app.add_send_observer(self.observer)
        self.app.register(bot, close=AsyncMock())

        self.assertEqual(await bot.send(self.target, Message.text("ok")), "receipt")

    async def test_real_onebot_client_counts_send_and_forward(self) -> None:
        bot = OneBot11Client("100", AsyncMock(return_value={"message_id": 1}))
        target = SessionTarget("onebot11", "100", "group", "300")
        self.app.add_send_observer(self.observer)
        self.app.register(bot, close=AsyncMock())

        await bot.send(target, Message.text("ok"))
        await bot.send_forward(target, [Message.text("ok")])
        self.assertEqual(self.observer.await_count, 2)
        self.observer.assert_awaited_with(bot, target)
