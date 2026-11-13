"""应用内业务对象缓存与明确的可关闭服务契约。"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod


class Service(ABC):
    """确有会话状态需要释放的对象；框架在任务退出后调用 close。"""

    @abstractmethod
    async def close(self) -> None:
        pass


def resolve_service(context, factory):
    from .application import ApplicationState

    if context.runtime.application.state not in {ApplicationState.STARTING, ApplicationState.RUNNING}:
        raise RuntimeError("应用未运行，不能创建业务服务")
    if not callable(factory) or inspect.iscoroutinefunction(factory):
        raise TypeError("业务对象工厂必须为同步函数")
    key = f"{factory.__module__}.{factory.__qualname__}"
    if key not in context.services:
        context.services[key] = factory(context.runtime)
    return context.services[key]
