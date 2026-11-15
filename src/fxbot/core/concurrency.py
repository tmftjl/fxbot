"""父调用独占子任务，返回或取消前等待全部子任务完成清理。"""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar
from collections.abc import Iterable, Coroutine

T = TypeVar("T")


async def run_concurrently(coroutines: Iterable[Coroutine[Any, Any, T]]) -> tuple[T, ...]:
    """仅为本次调用创建任务；取消不重复打断子任务的 finally。"""
    tasks: list[asyncio.Task[T]] = []
    completion = None
    try:
        for coroutine in coroutines:
            tasks.append(asyncio.create_task(coroutine))
        completion = asyncio.gather(*tasks)
        return tuple(await asyncio.shield(completion))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if completion is not None and completion.done() and not completion.cancelled():
            completion.exception()
