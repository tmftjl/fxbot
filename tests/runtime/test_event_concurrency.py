"""实际协议入口的逐事件任务、并发与关闭边界，不连接外部服务。"""

from __future__ import annotations

import asyncio
import unittest
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
        self.settings = Settings("test")
        self.connection = ProtocolConnection(
            self.app, OneBot11Protocol(), "100", self.settings, self.sent.put, AsyncMock()
        )
        self.connection.start()

    def receive(self, message_id):
        self.connection.receive({**payload(), "message_id": message_id})

    async def drain(self, timeout: float = 1) -> None:
        tasks = tuple(self.connection._tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)

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
        await self.drain()

    async def test_all_events_start_without_queue_or_concurrency_limit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        active, peak = 0, 0

        async def handler(ctx):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 10:
                entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

        self.app.router.message("capture", handler)
        for message_id in range(1, 11):
            self.receive(message_id)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(peak, 10)
        release.set()
        await self.drain()

    async def test_events_do_not_block_action_responses(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def handler(ctx):
            entered.set()
            await release.wait()

        self.app.router.message("capture", handler)
        self.receive(1)
        await asyncio.wait_for(entered.wait(), 1)
        action = asyncio.create_task(self.connection.client.call("get_login_info"))
        packet = await asyncio.wait_for(self.sent.get(), 1)
        self.connection.receive({"echo": packet["echo"], "status": "ok", "retcode": 0, "data": {"user_id": 100}})
        self.assertEqual(await asyncio.wait_for(action, 1), {"user_id": 100})
        release.set()
        await self.drain()

    async def test_disconnect_preserves_tasks_until_application_shutdown(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        started, cleaned = [], []

        async def handler(ctx):
            started.append(ctx.event.message_id)
            if len(started) == 3:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.append(ctx.event.message_id)

        self.app.router.message("capture", handler)
        for message_id in (1, 2, 3):
            self.receive(message_id)
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(self.connection.close())
        await asyncio.wait_for(closing, 1)
        self.assertFalse(cleaning.is_set())
        self.assertFalse(self.app.bots)
        stopping = asyncio.create_task(self.app.stop())
        await asyncio.wait_for(cleaning.wait(), 1)
        self.assertFalse(stopping.done())
        release.set()
        await asyncio.wait_for(stopping, 1)
        self.assertEqual(set(started), {"1", "2", "3"})
        self.assertEqual(set(cleaned), {"1", "2", "3"})
        self.assertFalse(self.connection._tasks)
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
        await asyncio.wait_for(closing, 1)
        self.assertFalse(cleaning.is_set())
        replacement = ProtocolConnection(self.app, OneBot11Protocol(), "100", self.settings, self.sent.put, AsyncMock())
        replacement.start()
        self.assertIs(self.app.get_bot("onebot11", "100"), replacement.client)
        self.assertFalse(self.app.is_registered(old.client))
        stopping = asyncio.create_task(self.app.stop())
        await asyncio.wait_for(cleaning.wait(), 1)
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        release.set()
        await asyncio.wait_for(asyncio.gather(closing, stopping), 1)
        self.assertFalse(self.app._retiring_connections)
        self.assertFalse(replacement._tasks)
