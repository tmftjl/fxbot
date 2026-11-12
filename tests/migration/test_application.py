"""自有应用的生命周期与任务回收测试。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from fxbot.core.application import Application


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    """覆盖启动失败、逆序清理、重复调用和在线身份。"""

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
        with self.assertLogs("fxbot.core.application", level="ERROR"):
            await application.stop()
        await application.stop()
        self.assertEqual(calls, ["start", "start", "second", "first"])
        self.assertEqual(application.state, "stopped")

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
        self.assertEqual(application.state, "stopped")

    async def test_stop_cancels_tasks_and_clears_bots(self) -> None:
        application = Application()
        await application.start()
        bot = SimpleNamespace(platform="test", bot_id="1")
        application.register(bot)
        with self.assertRaises(ValueError):
            application.register(bot)
        application.unregister(SimpleNamespace(platform="test", bot_id="1"))
        self.assertEqual(application.bots, (bot,))
        task = application.spawn(asyncio.Event().wait(), name="test")
        await asyncio.sleep(0)
        await application.stop()
        self.assertTrue(task.cancelled())
        self.assertEqual(application.bots, ())
        with self.assertRaises(RuntimeError):
            application.spawn(asyncio.Event().wait(), name="rejected")
