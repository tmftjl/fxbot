"""聊天维护任务的原生调度定义。"""

from __future__ import annotations

from collections.abc import Callable, Awaitable

from ...config import ConfigManager
from ...core.scheduler import Job, Interval, Scheduler

Callback = Callable[[], Awaitable[None]]


class MemoryMaintenance:
    """显式注入业务回调，定义任务时不加载聊天运行链路。"""

    def __init__(
        self,
        config: ConfigManager,
        scheduler: Scheduler,
        *,
        calendar: Callback,
        lifecycle: Callback,
        emoji_reconcile: Callback,
        emoji_cleanup: Callback,
    ) -> None:
        """绑定共享调度器和维护任务。"""
        self.config = config
        self.scheduler = scheduler
        self.calendar = calendar
        self.lifecycle = lifecycle
        self.emoji_reconcile = emoji_reconcile
        self.emoji_cleanup = emoji_cleanup

    def configure(self) -> None:
        """根据已校验配置重排，保持相同任务的运行实例。"""
        emoji = self.config.get_system()["chat"]["emoji"]
        self.scheduler.set(Job("chat:calendar", Interval(60), self.calendar, immediate=True))
        self.scheduler.set(Job("chat:lifecycle", Interval(60), self.lifecycle, immediate=True))
        self.scheduler.set(
            Job("chat:emoji_reconcile", Interval(max(60, emoji["check_interval"] * 60)), self.emoji_reconcile)
        )
        self.scheduler.set(
            Job(
                "chat:emoji_cleanup",
                Interval(max(60, emoji["cache_cleanup"]["check_interval_hours"] * 3600)),
                self.emoji_cleanup,
            )
        )

    def stop(self) -> None:
        """注销自己注册的任务，不影响会员或其他业务的调度。"""
        for name in ("chat:calendar", "chat:lifecycle", "chat:emoji_reconcile", "chat:emoji_cleanup"):
            self.scheduler.remove(name)
