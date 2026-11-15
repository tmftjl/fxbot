"""应用拥有的非重叠定时任务和显式重排。"""

from __future__ import annotations

import math
import asyncio
from typing import Any, Protocol
from datetime import datetime, timezone, timedelta
from functools import partial
from dataclasses import dataclass
from collections.abc import Callable, Coroutine

from .application import Application

Callback = Callable[[], Coroutine[Any, Any, None]]


class Schedule(Protocol):
    """调度策略只计算下一次执行的等待秒数。"""

    def delay(self, now: datetime) -> float:
        """根据带时区时刻计算下一次执行时间。"""


@dataclass(frozen=True, slots=True)
class Interval:
    """按固定时间间隔调度，不补跑错过的轮次。"""

    seconds: float

    def __post_init__(self) -> None:
        """拒绝无界或非正时间。"""
        if not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("任务间隔必须为有限正数")

    def delay(self, now: datetime) -> float:
        """间隔时间不受墙钟或时区切换影响。"""
        return self.seconds


@dataclass(frozen=True, slots=True)
class Daily:
    """在指定时区的若干小时执行同一分钟的任务。"""

    hours: tuple[int, ...]
    minute: int
    zone: timezone = timezone.utc

    def __post_init__(self) -> None:
        """校验明确的日历时刻，不接受任意 cron 表达式。"""
        if not self.hours or any(type(hour) is not int or not 0 <= hour <= 23 for hour in self.hours):
            raise ValueError("任务小时必须位于 0 到 23")
        if type(self.minute) is not int or not 0 <= self.minute <= 59:
            raise ValueError("任务分钟必须位于 0 到 59")

    def delay(self, now: datetime) -> float:
        """返回当前日历时区里下一个尚未到达的执行时刻。"""
        local = now.astimezone(self.zone)
        candidates = [local.replace(hour=hour, minute=self.minute, second=0, microsecond=0) for hour in self.hours]
        next_run = min(value if value > local else value + timedelta(days=1) for value in candidates)
        return (next_run - local).total_seconds()


@dataclass(frozen=True, slots=True)
class Job:
    """一项有稳定名称的异步业务任务。"""

    name: str
    schedule: Schedule
    callback: Callback
    immediate: bool = False


@dataclass(slots=True)
class _ScheduledJob:
    """调度器独占的下一轮时刻和当前执行句柄。"""

    spec: Job | None
    deadline: float
    running: asyncio.Task | None = None
    revision: int = 0


class Scheduler:
    """只负责确定执行时机，所有任务仍归应用统一回收。"""

    def __init__(self, application: Application) -> None:
        """绑定应用和实例私有任务表。"""
        self.application = application
        self._jobs: dict[str, _ScheduledJob] = {}
        self._changed = asyncio.Event()

    @property
    def jobs(self) -> tuple[Job, ...]:
        """返回当前任务定义的只读快照。"""
        return tuple(item.spec for item in self._jobs.values() if item.spec is not None)

    def set(self, job: Job) -> None:
        """相同定义保持原计划，变更定义取消旧执行并重新排期。"""
        previous = self._jobs.get(job.name)
        if previous is not None and previous.spec == job:
            return
        now = datetime.now(timezone.utc)
        delay = 0 if job.immediate else job.schedule.delay(now)
        deadline = asyncio.get_running_loop().time() + delay
        if previous is None:
            self._jobs[job.name] = _ScheduledJob(job, deadline)
        else:
            previous.spec = job
            previous.deadline = deadline
            previous.revision += 1
            if previous.running is not None:
                self.application.cancel(previous.running)
        self._changed.set()

    def remove(self, name: str) -> None:
        """移除任务并取消尚未结束的执行。"""
        previous = self._jobs.get(name)
        if previous is not None:
            previous.spec = None
            previous.revision += 1
            if previous.running is None:
                self._jobs.pop(name)
            else:
                self.application.cancel(previous.running)
        self._changed.set()

    def _finished(self, name: str, slot: _ScheduledJob, revision: int, task: asyncio.Task) -> None:
        """旧执行完成后才释放同名槽位，过期的周期不补跑。"""
        if self._jobs.get(name) is not slot or slot.running is not task:
            return
        slot.running = None
        if slot.spec is None:
            self._jobs.pop(name)
        elif slot.revision == revision:
            now = asyncio.get_running_loop().time()
            if slot.deadline <= now:
                slot.deadline = now + slot.spec.schedule.delay(datetime.now(timezone.utc))
        self._changed.set()

    async def remove_and_wait(self, name: str) -> None:
        """停止定义并等待该任务结束，包括其异步清理。"""
        slot = self._jobs.get(name)
        running = slot.running if slot is not None else None
        self.remove(name)
        if running is not None:
            results = await asyncio.gather(running, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result

    async def run(self) -> None:
        """调度定义快照，同一名称的任务不并发执行。"""
        loop = asyncio.get_running_loop()
        while True:
            self._changed.clear()
            now = loop.time()
            for slot in self._jobs.values():
                spec = slot.spec
                if spec is not None and slot.running is None and slot.deadline <= now:
                    revision = slot.revision
                    task = self.application.spawn(spec.callback(), name=f"job:{spec.name}")
                    slot.running = task
                    slot.deadline = now + spec.schedule.delay(datetime.now(timezone.utc))
                    task.add_done_callback(partial(self._finished, spec.name, slot, revision))
            deadlines = [
                slot.deadline for slot in self._jobs.values() if slot.spec is not None and slot.running is None
            ]
            delay = max(0, min(deadlines) - loop.time()) if deadlines else None
            changed = asyncio.create_task(self._changed.wait(), name="scheduler.changed")
            try:
                await asyncio.wait((changed,), timeout=delay)
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
