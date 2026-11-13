"""模块级定时业务声明，执行和回收由核心调度器负责。"""

from __future__ import annotations

import inspect
from typing import Any
from dataclasses import dataclass
from collections.abc import Mapping

from .scheduler import Interval, Schedule


@dataclass(frozen=True, slots=True)
class Scheduled:
    interval: Schedule | None
    run_on_start: bool
    name: str
    plan: Any = None
    watch_config: bool = False


@dataclass(frozen=True, slots=True)
class TaskContext:
    notifications: Any
    state: dict[str, Any]
    services: Mapping[str, Any]
    runtime: Any = None

    def service(self, factory):
        from .services import resolve_service

        return resolve_service(self, factory)

    @property
    def first_run(self) -> bool:
        return not self.state.get("completed", False)


def scheduled(
    *, interval: float | None = None, plan=None, watch_config: bool = False, run_on_start: bool = False, name: str = ""
):
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

    def decorate(handler):
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("定时处理器必须是异步函数")
        if hasattr(handler, "__fxbot_schedule__"):
            raise ValueError("定时处理器不能重复声明")
        handler.__fxbot_schedule__ = Scheduled(schedule, run_on_start, name or handler.__name__, plan, watch_config)
        return handler

    return decorate


def collect_schedules(modules):
    result = []
    seen = set()
    for module in modules:
        for handler in vars(module).values():
            if not inspect.isfunction(handler) or handler.__module__ != module.__name__ or handler in seen:
                continue
            declaration = getattr(handler, "__fxbot_schedule__", None)
            if isinstance(declaration, Scheduled):
                seen.add(handler)
                result.append((handler, declaration))
    return tuple(result)
