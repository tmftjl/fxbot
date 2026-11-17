"""适配器注册表：连接器、入站 matcher 和下行 dispatch 只通过这里查找平台。"""

from __future__ import annotations

from loguru import logger

from fxbot.core import Event

from .qq import QQAdapter
from .base import Adapter
from .onebot11 import OneBot11Adapter
from .onebot12 import OneBot12Adapter

_ADAPTERS: list[Adapter] = []


def register(adapter: Adapter) -> Adapter:
    """按 key 注册；重复注册替换旧实现，保持注册顺序。"""
    if not isinstance(adapter, Adapter):
        raise TypeError("适配器必须实现 Adapter")
    for index, existing in enumerate(_ADAPTERS):
        if existing.key == adapter.key:
            _ADAPTERS[index] = adapter
            return adapter
    _ADAPTERS.append(adapter)
    return adapter


def unregister(adapter: Adapter) -> None:
    """移除指定适配器，主要供扩展测试清理。"""
    try:
        _ADAPTERS.remove(adapter)
    except ValueError:
        return


def adapters() -> tuple[Adapter, ...]:
    """返回注册顺序的只读快照。"""
    return tuple(_ADAPTERS)


def find_event(event: Event) -> Adapter | None:
    """按注册顺序查找入站适配器，未知平台只记录 debug。"""
    for adapter in tuple(_ADAPTERS):
        if adapter.matches(event):
            return adapter
    logger.debug("[gscore] 没有适配器处理入站平台: {}", event.platform)
    return None


def find_send(bot_id: str) -> Adapter | None:
    """按注册顺序查找下行适配器，未知 bot_id 只记录 debug。"""
    for adapter in tuple(_ADAPTERS):
        if adapter.matches_send(bot_id):
            return adapter
    logger.debug("[gscore] 没有适配器处理下行 bot_id: {}", bot_id)
    return None


for _adapter in (OneBot11Adapter(), OneBot12Adapter(), QQAdapter()):
    register(_adapter)
