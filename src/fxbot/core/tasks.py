"""模块级定时业务声明，执行和回收由核心调度器负责。"""

from __future__ import annotations

import inspect
from types import ModuleType
from typing import TypeVar
from dataclasses import dataclass
from collections.abc import Callable, Iterable, Awaitable

from .services import ServiceScope
from .contracts import Notifications, ApplicationServices
from .scheduler import Job, Interval, Schedule

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Scheduled:
    interval: Schedule | None
    run_on_start: bool
    name: str
    plan: Callable[[TaskContext], Schedule | None] | None = None
    watch_config: bool = False


@dataclass(frozen=True, slots=True)
class TaskContext:
    notifications: Notifications
    state: dict[str, object]
    scope: ServiceScope
    app_services: ApplicationServices
    _execution: _TaskExecution

    def service(self, factory: Callable[[ApplicationServices], T]) -> T:
        return self.scope.resolve(factory, self.app_services)

    @property
    def first_run(self) -> bool:
        return not self._execution.completed


@dataclass(slots=True)
class _TaskExecution:
    completed: bool = False


class ScheduledTask:
    """排期与执行状态属于本应用，业务字典不存框架标志。"""

    def __init__(
        self,
        name: str,
        handler: ScheduledHandler,
        declaration: Scheduled,
        services: ServiceScope,
        runtime: ApplicationServices,
    ) -> None:
        self.name = name
        self.handler = handler
        self.declaration = declaration
        self.context = TaskContext(runtime.notifications, {}, services, runtime, _TaskExecution())

    def configure(self) -> None:
        declaration = self.declaration
        schedule = declaration.plan(self.context) if declaration.plan is not None else declaration.interval
        scheduler = self.context.app_services.scheduler
        if schedule is None:
            scheduler.remove(self.name)
        else:
            scheduler.set(Job(self.name, schedule, self.run, declaration.run_on_start))

    async def run(self) -> None:
        await self.handler(self.context)
        self.context._execution.completed = True

    def clear(self) -> None:
        self.context.state.clear()


Plan = Callable[[TaskContext], Schedule | None]
ScheduledHandler = Callable[[TaskContext], Awaitable[None]]


def scheduled(
    *,
    interval: float | None = None,
    plan: Plan | None = None,
    watch_config: bool = False,
    run_on_start: bool = False,
    name: str = "",
) -> Callable[[ScheduledHandler], ScheduledHandler]:
    """声明不重叠运行的周期任务，不让业务管理后台 Task。"""
    if (interval is None) == (plan is None):
        raise ValueError("必须且只能指定 interval 或 plan")
    if plan is not None and (not callable(plan) or inspect.iscoroutinefunction(plan)):
        raise TypeError("plan 必须为同步排期函数")
    if type(watch_config) is not bool:
        raise TypeError("watch_config 必须为布尔值")
    schedule = Interval(interval) if interval is not None else None
    if type(run_on_start) is not bool:
        raise TypeError("run_on_start 必须为布尔值")
    if not isinstance(name, str) or ":" in name:
        raise ValueError("任务名称必须是不含冒号的字符串")

    def decorate(handler: ScheduledHandler) -> ScheduledHandler:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("定时处理器必须是异步函数")
        if hasattr(handler, "__fxbot_schedule__"):
            raise ValueError("定时处理器不能重复声明")
        setattr(
            handler,
            "__fxbot_schedule__",
            Scheduled(schedule, run_on_start, name or handler.__name__, plan, watch_config),
        )
        return handler

    return decorate


def collect_schedules(modules: Iterable[ModuleType]) -> tuple[tuple[ScheduledHandler, Scheduled], ...]:
    result: list[tuple[ScheduledHandler, Scheduled]] = []
    seen: set[ScheduledHandler] = set()
    for module in modules:
        for handler in vars(module).values():
            if not inspect.isfunction(handler) or handler.__module__ != module.__name__ or handler in seen:
                continue
            declaration = getattr(handler, "__fxbot_schedule__", None)
            if isinstance(declaration, Scheduled):
                seen.add(handler)
                result.append((handler, declaration))
    return tuple(result)
