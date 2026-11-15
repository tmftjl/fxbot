"""按工厂对象身份隔离的应用服务作用域。"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import TypeVar, cast
from contextlib import AsyncExitStack
from dataclasses import dataclass
from collections.abc import Callable

from .contracts import ApplicationServices

T = TypeVar("T")


class Service(ABC):
    """有业务状态需要释放的服务，框架在任务退出后调用 close。"""

    @abstractmethod
    async def close(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _Entry:
    factory: object
    instance: object


class ServiceScope:
    """强引用工厂，按身份缓存；不依赖名称、哈希或自定义相等判断。"""

    def __init__(self) -> None:
        self._entries: dict[int, _Entry] = {}
        self._closed = False

    def resolve(self, factory: Callable[[ApplicationServices], T], runtime: ApplicationServices) -> T:
        from .application import ApplicationState

        if self._closed or runtime.application.state not in {ApplicationState.STARTING, ApplicationState.RUNNING}:
            raise RuntimeError("应用未运行，不能获取业务服务")
        if not callable(factory) or inspect.iscoroutinefunction(factory) or inspect.isasyncgenfunction(factory):
            raise TypeError("业务对象工厂必须为同步函数")
        key = id(factory)
        entry = self._entries.get(key)
        if entry is None:
            instance = factory(runtime)
            if inspect.isawaitable(instance):
                if inspect.iscoroutine(instance):
                    instance.close()
                raise TypeError("业务对象工厂不能返回协程")
            entry = _Entry(factory, instance)
            self._entries[key] = entry
        return cast(T, entry.instance)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with AsyncExitStack() as cleanup:
            cleanup.callback(self._entries.clear)
            seen: set[int] = set()
            for entry in self._entries.values():
                instance = entry.instance
                if isinstance(instance, Service) and id(instance) not in seen:
                    seen.add(id(instance))
                    cleanup.push_async_callback(instance.close)
