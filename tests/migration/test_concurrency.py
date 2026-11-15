"""响应器分组并发、优先级屏障与子任务所有权回归。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fxbot.core import Router, current_event
from fxbot.core.concurrency import run_concurrently
from tests.migration.test_router import event


class ConcurrentRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.router = Router()
        self.bot = SimpleNamespace(platform="test", bot_id="bot", send=AsyncMock())

    async def test_same_priority_can_enter_before_either_finishes_and_lower_waits(self):
        first, second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        lower = AsyncMock()

        async def one(ctx):
            first.set()
            await second.wait()
            await release.wait()

        async def two(ctx):
            second.set()
            await first.wait()
            await release.wait()

        self.router.message("one", one, block=False)
        self.router.message("two", two, block=False)
        self.router.message("lower", lower, priority=99)
        task = asyncio.create_task(self.router.dispatch(self.bot, event()))
        self.addAsyncCleanup(self.cancel, task)
        await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), 1)
        lower.assert_not_awaited()
        release.set()
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.handled, ("one", "two", "lower"))

    async def test_blocking_sibling_does_not_cancel_other_same_priority(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        lower = AsyncMock()

        async def fast(ctx):
            await ctx.finish()

        async def slow(ctx):
            entered.set()
            await release.wait()
            finished.set()

        self.router.message("fast", fast)
        self.router.message("slow", slow, block=False)
        self.router.message("lower", lower, priority=99)
        task = asyncio.create_task(self.router.dispatch(self.bot, event()))
        self.addAsyncCleanup(self.cancel, task)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(task.done())
        release.set()
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.handled, ("fast", "slow"))
        self.assertTrue(result.blocked)
        self.assertTrue(finished.is_set())
        lower.assert_not_awaited()

    async def test_failure_isolated_from_siblings_but_stops_lower_priority(self):
        succeeded, lower = AsyncMock(), AsyncMock()
        self.router.message("broken", AsyncMock(side_effect=ValueError("bad")), block=False)
        self.router.message("sibling", succeeded, block=False)
        self.router.message("lower", lower, priority=99)
        with self.assertLogs("fxbot.core.router", level="ERROR"):
            result = await self.router.dispatch(self.bot, event())
        self.assertEqual(result.handled, ("broken", "sibling"))
        self.assertEqual(result.failed, ("broken",))
        succeeded.assert_awaited_once()
        lower.assert_not_awaited()

    async def test_parent_cancel_waits_for_all_sibling_cleanup(self):
        entered = [asyncio.Event(), asyncio.Event()]
        cleanup = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()
        finished = []

        for index in range(2):

            async def handler(ctx, index=index):
                entered[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup[index].set()
                    await release.wait()
                    finished.append(index)

            self.router.message(str(index), handler)
        task = asyncio.create_task(self.router.dispatch(self.bot, event()))
        self.addAsyncCleanup(self.cancel, task)
        self.addCleanup(release.set)
        await asyncio.wait_for(asyncio.gather(*(item.wait() for item in entered)), 1)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(*(item.wait() for item in cleanup)), 1)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(set(finished), {0, 1})
        with self.assertRaises(RuntimeError):
            current_event()

    async def test_state_is_private_to_each_sibling(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = event()

        async def one(ctx):
            self.assertIs(current_event(), original)
            ctx.state["private"] = True
            entered.set()
            await release.wait()

        async def two(ctx):
            await entered.wait()
            self.assertNotIn("private", ctx.state)
            self.assertIs(current_event(), original)
            release.set()

        self.router.message("one", one)
        self.router.message("two", two)
        await asyncio.wait_for(self.router.dispatch(self.bot, original), 1)

    async def cancel(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class OwnedConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_iterator_failure_reclaims_already_created_children(self):
        before = set(asyncio.all_tasks())

        async def blocked():
            await asyncio.Event().wait()

        def generate():
            yield blocked()
            raise ValueError("iterator-failure")

        with self.assertRaisesRegex(ValueError, "iterator-failure"):
            await run_concurrently(generate())
        await asyncio.sleep(0)
        self.assertEqual(set(asyncio.all_tasks()), before)

    async def test_results_keep_input_order_not_completion_order(self):
        release = asyncio.Event()

        async def first():
            await release.wait()
            return "first"

        async def second():
            release.set()
            return "second"

        self.assertEqual(await run_concurrently((first(), second())), ("first", "second"))
        self.assertEqual(await run_concurrently(()), ())

    async def test_failed_child_cancels_and_joins_other_children(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def failure():
            await entered.wait()
            raise ValueError("child-failure")

        async def blocked():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        with self.assertRaisesRegex(ValueError, "child-failure"):
            await run_concurrently((failure(), blocked()))
        self.assertTrue(cleaned.is_set())
