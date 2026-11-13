"""原生调度策略和任务所有权测试。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone, timedelta

from fxbot.core.scheduler import Job, Daily, Interval, Scheduler
from fxbot.core.application import Application


class ScheduleTests(unittest.TestCase):
    """日历时区和固定间隔由各自策略明确计算。"""

    def test_daily_uses_explicit_timezone(self) -> None:
        strategy = Daily((8, 12, 16, 20), 5, timezone(timedelta(hours=8)))
        self.assertEqual(strategy.delay(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)), 300)
        self.assertEqual(strategy.delay(datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)), 12 * 3600)

    def test_interval_is_positive_and_finite(self) -> None:
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Interval(value)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    """不重叠、不重放，移除任务时中止旧执行。"""

    async def test_periodic_job_keeps_running_without_overlap(self) -> None:
        app = Application()
        await app.start()
        scheduler = Scheduler(app)
        complete = asyncio.Event()
        active = maximum = count = 0

        async def callback() -> None:
            nonlocal active, maximum, count
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.01)
                count += 1
                if count >= 3:
                    complete.set()
            finally:
                active -= 1

        scheduler.set(Job("periodic", Interval(0.002), callback, immediate=True))
        app.spawn(scheduler.run(), name="scheduler")
        try:
            await asyncio.wait_for(complete.wait(), 1)
            self.assertEqual(maximum, 1)
        finally:
            await app.stop()

    async def test_repeated_reconfiguration_starts_only_latest_definition(self) -> None:
        app = Application()
        await app.start()
        scheduler = Scheduler(app)
        entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))
        latest, barrier, interrupted = (asyncio.Event() for _ in range(3))
        calls = []

        async def previous() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
                finished.set()

        async def superseded() -> None:
            calls.append("superseded")

        async def replacement() -> None:
            calls.append("latest")
            latest.set()

        async def probe() -> None:
            barrier.set()

        app.spawn(scheduler.run(), name="scheduler")
        scheduler.set(Job("target", Interval(3600), previous, immediate=True))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            scheduler.set(Job("target", Interval(3600), superseded, immediate=True))
            await asyncio.wait_for(cleaning.wait(), 1)
            scheduler.remove("target")
            scheduler.set(Job("target", Interval(3600), replacement, immediate=True))
            scheduler.set(Job("probe", Interval(3600), probe, immediate=True))
            await asyncio.wait_for(barrier.wait(), 1)
            self.assertFalse(interrupted.is_set())
            self.assertEqual(calls, [])
            release.set()
            await asyncio.wait_for(latest.wait(), 1)
            self.assertEqual(calls, ["latest"])
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            await app.stop()

    async def test_replacement_waits_for_previous_execution_cleanup(self) -> None:
        for remove_first in (False, True):
            with self.subTest(remove_first=remove_first):
                app = Application()
                await app.start()
                scheduler = Scheduler(app)
                entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))
                replacement_started, barrier = asyncio.Event(), asyncio.Event()

                async def previous() -> None:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaning.set()
                        await release.wait()
                        finished.set()

                async def replacement() -> None:
                    replacement_started.set()

                async def reached_barrier() -> None:
                    barrier.set()

                app.spawn(scheduler.run(), name="scheduler")
                scheduler.set(Job("target", Interval(3600), previous, immediate=True))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    if remove_first:
                        scheduler.remove("target")
                    scheduler.set(Job("target", Interval(3600), replacement, immediate=True))
                    await asyncio.wait_for(cleaning.wait(), 1)
                    scheduler.set(Job("barrier", Interval(3600), reached_barrier, immediate=True))
                    await asyncio.wait_for(barrier.wait(), 1)
                    self.assertFalse(replacement_started.is_set(), "旧任务清理结束前不能启动同名新任务")
                    release.set()
                    await asyncio.wait_for(replacement_started.wait(), 1)
                    self.assertTrue(finished.is_set())
                finally:
                    release.set()
                    await asyncio.wait_for(finished.wait(), 1)
                    await app.stop()

    async def test_application_stop_does_not_cancel_pending_job_cleanup_twice(self) -> None:
        app = Application()
        await app.start()
        scheduler = Scheduler(app)
        entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
        probe_entered, probe_stopped, interrupted = (asyncio.Event() for _ in range(3))

        async def callback() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
                cleaned.set()

        async def probe() -> None:
            probe_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                probe_stopped.set()

        app.spawn(scheduler.run(), name="scheduler")
        app.spawn(probe(), name="probe")
        scheduler.set(Job("target", Interval(3600), callback, immediate=True))
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(probe_entered.wait(), 1)
            scheduler.remove("target")
            await asyncio.wait_for(cleaning.wait(), 1)
            stopping = asyncio.create_task(app.stop())
            await asyncio.wait_for(probe_stopped.wait(), 1)
            self.assertFalse(interrupted.is_set(), "应用关闭不能再次取消已在清理的任务")
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.wait_for(stopping, 1)
            self.assertTrue(cleaned.is_set())
        finally:
            release.set()
            if stopping is not None:
                await asyncio.wait_for(stopping, 1)
            else:
                await app.stop()

    async def test_remove_cancels_running_job_and_application_owns_loop(self) -> None:
        application = Application()
        await application.start()
        scheduler = Scheduler(application)
        entered, exited = asyncio.Event(), asyncio.Event()

        async def callback() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        job = Job("test", Interval(1), callback, immediate=True)
        scheduler.set(job)
        scheduler.set(job)
        loop = application.spawn(scheduler.run(), name="scheduler")
        await asyncio.wait_for(entered.wait(), 1)
        scheduler.remove("test")
        await asyncio.wait_for(exited.wait(), 1)
        await application.stop()
        self.assertTrue(loop.done())
