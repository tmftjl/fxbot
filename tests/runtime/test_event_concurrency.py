"""实际协议入口的事件并发、容量和关闭边界，不连接外部服务。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fxbot.core.application import Application
from fxbot.runtime.settings import Settings
from fxbot.runtime.connection import ProtocolConnection
from tests.migration.test_onebot11 import payload
from fxbot.platforms.onebot11.protocol import OneBot11Protocol


class EventConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = Application()
        await self.app.start()
        self.addAsyncCleanup(self.app.stop)
        self.sent = asyncio.Queue()
        self.settings = Settings("test", event_concurrency=2, queue_size=3)
        self.connection = ProtocolConnection(
            self.app, OneBot11Protocol(), "100", self.settings, self.sent.put, AsyncMock()
        )
        self.connection.start()

    def receive(self, message_id):
        self.connection.receive({**payload(), "message_id": message_id})

    async def test_later_message_finishes_while_first_waits(self):
        slow, fast, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def handler(ctx):
            if ctx.event.message_id == "1":
                slow.set()
                await release.wait()
            else:
                fast.set()

        self.app.router.message("capture", handler)
        self.receive(1)
        await asyncio.wait_for(slow.wait(), 1)
        self.receive(2)
        await asyncio.wait_for(fast.wait(), 1)
        self.assertFalse(release.is_set())
        release.set()
        await asyncio.wait_for(self.connection._queue.join(), 1)

    async def test_concurrency_and_queue_are_bounded_without_blocking_action_responses(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        active, peak = 0, 0

        async def handler(ctx):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

        self.app.router.message("capture", handler)
        self.receive(1)
        self.receive(2)
        await asyncio.wait_for(entered.wait(), 1)
        for message_id in (3, 4, 5):
            self.receive(message_id)
        self.assertEqual(self.connection._queue.qsize(), 3)
        with self.assertRaises(asyncio.QueueFull):
            self.receive(6)
        action = asyncio.create_task(self.connection.client.call("get_login_info"))
        packet = await asyncio.wait_for(self.sent.get(), 1)
        self.connection.receive({"echo": packet["echo"], "status": "ok", "retcode": 0, "data": {"user_id": 100}})
        self.assertEqual(await asyncio.wait_for(action, 1), {"user_id": 100})
        self.assertEqual(active, 2)
        release.set()
        await asyncio.wait_for(self.connection._queue.join(), 1)
        self.assertEqual(peak, 2)

    async def test_close_joins_all_inflight_cleanup_and_discards_unstarted_events(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        started, cleaned = [], []

        async def handler(ctx):
            started.append(ctx.event.message_id)
            if len(started) == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.append(ctx.event.message_id)

        self.app.router.message("capture", handler)
        self.receive(1)
        self.receive(2)
        await asyncio.wait_for(entered.wait(), 1)
        self.receive(3)
        closing = asyncio.create_task(self.connection.close())
        await asyncio.wait_for(cleaning.wait(), 1)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.wait_for(closing, 1)
        await asyncio.wait_for(self.connection._queue.join(), 1)
        self.assertEqual(set(started), {"1", "2"})
        self.assertEqual(set(cleaned), {"1", "2"})
        self.assertFalse(self.app.bots)

    async def test_reconnect_can_replace_retiring_identity_and_shutdown_still_owns_old_cleanup(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def handler(ctx):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        self.app.router.message("capture", handler)
        self.receive(1)
        await asyncio.wait_for(entered.wait(), 1)
        old = self.connection
        closing = asyncio.create_task(old.close())
        await asyncio.wait_for(cleaning.wait(), 1)
        replacement = ProtocolConnection(self.app, OneBot11Protocol(), "100", self.settings, self.sent.put, AsyncMock())
        replacement.start()
        self.assertIs(self.app.get_bot("onebot11", "100"), replacement.client)
        self.assertFalse(self.app.is_registered(old.client))
        stopping = asyncio.create_task(self.app.stop())
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        release.set()
        await asyncio.wait_for(asyncio.gather(closing, stopping), 1)
        self.assertFalse(self.app._retiring_connections)
        self.assertTrue(replacement._worker.done())


class ConcurrencySettingsTests(unittest.TestCase):
    def test_default_and_invalid_concurrency(self):
        self.assertEqual(Settings("test").event_concurrency, 8)
        for value in (True, 0, -1, 1.5, "8", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings("test", event_concurrency=value)

    def test_concurrency_is_read_from_project_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"token":"test","event_concurrency":3}', encoding="utf-8")
            self.assertEqual(Settings.load(path).event_concurrency, 3)
