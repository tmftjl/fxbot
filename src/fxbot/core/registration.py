"""插件导入失败时回收已声明的扩展注册，不绑定具体业务注册表。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable, Iterator

_pending: ContextVar[list[Callable[[], None]] | None] = ContextVar("plugin_registrations", default=None)


def track_registration(remove: Callable[[], None]) -> None:
    pending = _pending.get()
    if pending is not None:
        pending.append(remove)


@contextmanager
def registration_scope() -> Iterator[None]:
    pending: list[Callable[[], None]] = []
    token = _pending.set(pending)
    try:
        yield
    except BaseException:
        for remove in reversed(pending):
            remove()
        raise
    finally:
        _pending.reset(token)
