"""自有应用的生命周期与任务回收测试。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fxbot.core.application import Application, ApplicationState


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    """覆盖启动失败、逆序清理、重复调用和在线身份。"""

    async def test_cancellation_cannot_target_another_applications_task(self) -> None:
        first, second = Application(), Application()
        await first.start()
        await second.start()
        task = first.spawn(asyncio.Event().wait(), name="owned-by-first")
        try:
            with self.assertRaises(ValueError):
                second.cancel(task)
            self.assertFalse(task.cancelled())
            self.assertTrue(first.cancel(task))
            self.assertFalse(first.cancel(task))
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(first.cancel(task))
        finally:
            await first.stop()
            await second.stop()

    async def test_resources_close_in_reverse_order_despite_errors(self) -> None:
        application = Application()
        calls = []

        async def start() -> None:
            calls.append("start")

        async def first() -> None:
            calls.append("first")

        async def second() -> None:
            calls.append("second")
            raise RuntimeError("cleanup failed")

        application.resource(start, first)
        application.resource(start, second)
        await application.start()
        await application.start()
        with self.assertRaises(RuntimeError):
            await application.stop()
        with self.assertRaises(RuntimeError):
            await application.stop()
        self.assertEqual(calls, ["start", "start", "second", "first"])
        self.assertEqual(application.state, ApplicationState.STOPPED)

    async def test_start_failure_releases_partially_started_resource(self) -> None:
        application = Application()
        calls = []

        async def start() -> None:
            raise RuntimeError("startup failed")

        async def stop() -> None:
            calls.append("closed")

        application.resource(start, stop)
        with self.assertRaises(RuntimeError):
            await application.start()
        self.assertEqual(calls, ["closed"])
        self.assertEqual(application.state, ApplicationState.STOPPED)

    async def test_stop_cancels_tasks_and_clears_bots(self) -> None:
        application = Application()
        await application.start()
        bot = SimpleNamespace(platform="test", bot_id="1")
        application.register(bot, close=AsyncMock())
        with self.assertRaises(ValueError):
            application.register(bot, close=AsyncMock())
        application.unregister(SimpleNamespace(platform="test", bot_id="1"))
        self.assertEqual(application.bots, (bot,))
        task = application.spawn(asyncio.Event().wait(), name="test")
        await asyncio.sleep(0)
        await application.stop()
        self.assertTrue(task.cancelled())
        self.assertEqual(application.bots, ())
        with self.assertRaises(RuntimeError):
            application.spawn(asyncio.Event().wait(), name="rejected")

    async def test_cancelled_stop_can_be_joined_without_losing_cleanup(self) -> None:
        application = Application()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def start() -> None:
            calls.append("start")

        async def stop() -> None:
            entered.set()
            await release.wait()
            calls.append("stop")

        application.resource(start, stop)
        await application.start()
        first = asyncio.create_task(application.stop())
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(application.stop())
        release.set()
        await second
        self.assertEqual(calls, ["start", "stop"])
        self.assertEqual(application.state, ApplicationState.STOPPED)

    async def test_stop_closes_all_registered_connections_despite_failure(self) -> None:
        application = Application()
        await application.start()
        calls = []

        async def failed() -> None:
            calls.append("failed")
            raise OSError("close failed")

        async def closed() -> None:
            calls.append("closed")

        application.register(SimpleNamespace(platform="test", bot_id="1"), close=failed)
        application.register(SimpleNamespace(platform="test", bot_id="2"), close=closed)
        with self.assertRaises(OSError):
            await application.stop()
        self.assertCountEqual(calls, ["failed", "closed"])
        self.assertEqual(application.bots, ())

    async def test_cancelled_start_releases_partial_resources(self) -> None:
        application = Application()
        entered = asyncio.Event()
        calls = []

        async def start() -> None:
            entered.set()
            await asyncio.Event().wait()

        async def stop() -> None:
            calls.append("closed")

        application.resource(start, stop)
        startup = asyncio.create_task(application.start())
        await entered.wait()
        startup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await startup
        await application.stop()
        self.assertEqual(calls, ["closed"])
        self.assertEqual(application.state, ApplicationState.STOPPED)

    async def test_state_is_read_only_and_stopped_application_cannot_restart(self) -> None:
        application = Application()
        with self.assertRaises(AttributeError):
            application.state = ApplicationState.RUNNING
        await application.stop()
        with self.assertRaises(RuntimeError):
            await application.start()
