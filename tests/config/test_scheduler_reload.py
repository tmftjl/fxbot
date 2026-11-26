"""原生调度的配置变更和订阅所有权测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fxbot.config import SYSTEM_DEFAULTS, ConfigManager
from fxbot.utils.cache import cache
from fxbot.core.scheduler import Scheduler
from fxbot.core.application import Application
from fxbot.membership.tasks import MembershipMaintenance
from fxbot.chat.memory.tasks import MemoryMaintenance


class SchedulerReloadTests(unittest.IsolatedAsyncioTestCase):
    """只使用应用和调度策略，不加载外部框架或后台线程。"""

    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="fxbot-native-scheduler-")
        self.addCleanup(self.directory.cleanup)
        self.manager = ConfigManager()
        self.proxy = self.manager.register("system")
        self.proxy.storage._path = Path(self.directory.name) / "config.json"
        cache.pop(self.proxy._cache_key)
        self.addCleanup(cache.pop, self.proxy._cache_key)
        self.config = deepcopy(SYSTEM_DEFAULTS)
        self.proxy.save(self.config)
        self.application = Application()
        await self.application.start()
        self.addAsyncCleanup(self.application.stop)
        self.scheduler = Scheduler(self.application)
        self.memory = MemoryMaintenance(
            self.manager,
            self.scheduler,
            calendar=AsyncMock(),
            lifecycle=AsyncMock(),
            emoji_reconcile=AsyncMock(),
            emoji_cleanup=AsyncMock(),
            memory_schedule=AsyncMock(),
        )
        self.membership = MembershipMaintenance(
            self.application, self.manager, AsyncMock(), AsyncMock(), self.scheduler
        )
        self.addCleanup(self.manager.subscribe(lambda: self.memory.configure()))
        self.addCleanup(self.manager.subscribe(lambda: self.membership.configure()))

    def jobs(self) -> dict:
        """按稳定任务名读取定义。"""
        return {job.name: job for job in self.scheduler.jobs}

    async def test_save_updates_intervals_without_duplicates(self) -> None:
        self.config["chat"]["emoji"]["check_interval"] = 17
        self.config["chat"]["emoji"]["cache_cleanup"]["check_interval_hours"] = 2.5
        self.manager.save_console_configs({"system": self.config})
        self.manager.save_console_configs({"system": self.config})
        jobs = self.jobs()
        self.assertEqual(jobs["chat:emoji_reconcile"].schedule.seconds, 1020)
        self.assertEqual(jobs["chat:emoji_cleanup"].schedule.seconds, 9000)
        self.assertEqual(len(jobs), 6)

    async def test_calendar_and_diagnostics_are_immediate_each_minute(self) -> None:
        self.memory.configure()
        self.memory.configure()
        self.assertTrue(self.jobs()["chat:memory_schedule"].immediate)
        self.assertEqual(self.jobs()["chat:memory_schedule"].schedule.seconds, 30)
        for name in ("chat:calendar", "chat:lifecycle"):
            self.assertTrue(self.jobs()[name].immediate)
            self.assertEqual(self.jobs()[name].schedule.seconds, 60)

    async def test_disable_removes_job_and_queued_callback_rereads_switch(self) -> None:
        self.membership.configure()
        queued = self.jobs()["membership:expiry"].callback
        self.config["membership"]["enable_scheduler"] = False
        self.manager.save_console_configs({"system": self.config})
        self.assertNotIn("membership:expiry", self.jobs())
        with patch.object(self.membership, "process", new_callable=AsyncMock) as process:
            await queued()
        process.assert_not_awaited()

    async def test_reschedule_uses_new_explicit_time(self) -> None:
        self.config["membership"]["schedule_time"] = "19:41"
        self.manager.save_console_configs({"system": self.config})
        schedule = self.jobs()["membership:expiry"].schedule
        self.assertEqual(schedule.hours, (19,))
        self.assertEqual(schedule.minute, 41)

    async def test_notification_failure_is_reported_without_rolling_back_saved_config(self) -> None:
        self.config["membership"]["schedule_time"] = "09:14"
        with patch.object(self.memory, "configure", side_effect=RuntimeError("scheduler unavailable")):
            with self.assertRaisesRegex(RuntimeError, "scheduler unavailable"):
                self.manager.save_console_configs({"system": self.config})
        self.assertEqual(self.manager.get_system()["membership"]["schedule_time"], "09:14")

    async def test_failed_save_does_not_notify(self) -> None:
        with patch.object(self.proxy, "save", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.manager.save_console_configs({"system": self.config})
        self.assertEqual(self.scheduler.jobs, ())
