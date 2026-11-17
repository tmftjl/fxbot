"""鸣潮角色列表本地缓存。"""

from __future__ import annotations

import os
import json
import time

from loguru import logger

from .alias import normalize, load_alias_map, build_alias_index, resolve_alias_path
from .client import WuwaEmojiError, fetch_characters
from .config import cfg_character_cache_ttl
from ...core.plugin import scheduled
from ...utils.cache import cache
from ...utils.paths import cache_dir

CACHE_PATH = cache_dir("wuwa_emoji") / "characters.json"


def _read_disk() -> tuple[float, list[dict[str, str]]] | None:
    """读取磁盘缓存，缺失或损坏时返回 None。"""
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        fetched_at = float(data["fetched_at"])
        items = data["items"]
    except Exception:
        logger.opt(exception=True).warning("[wuwa_emoji] 角色缓存读取失败，将重新拉取")
        return None
    if not isinstance(items, list):
        return None
    return fetched_at, [item for item in items if isinstance(item, dict)]


def _write_disk(fetched_at: float, items: list[dict[str, str]]) -> None:
    """原子写入磁盘缓存，避免留下半截文件。"""
    tmp_path = CACHE_PATH.with_name(f"{CACHE_PATH.name}.tmp")
    try:
        payload = {"fetched_at": fetched_at, "items": items}
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, CACHE_PATH)
    except Exception:
        logger.opt(exception=True).warning("[wuwa_emoji] 角色缓存写入失败")
        tmp_path.unlink(missing_ok=True)


def _remember(fetched_at: float, items: list[dict[str, str]]) -> tuple[float, list[dict[str, str]]]:
    """更新角色列表缓存，并作废别名索引。

    别名索引与角色列表必须同源，分开更新会出现「别名指向已下架角色」的窗口，
    所以所有对角色列表缓存的写入都收口到这里、并强制下一次查询重建索引。
    """
    entry = (fetched_at, items)
    cache.set(("wuwa", "characters"), entry)
    cache.clear_prefix(("wuwa", "alias"))
    return entry


def _sync_alias_index(items: list[dict[str, str]]) -> dict[str, str]:
    """按来源文件的路径与 mtime 重建别名索引。"""
    path = resolve_alias_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    key = ("wuwa", "alias", str(path))
    entry = cache.get(key)
    if entry is not None and entry[0] == mtime:
        return entry[1]
    index = build_alias_index(items, load_alias_map(path))
    cache.set(key, (mtime, index))
    return index


async def refresh_characters() -> list[dict[str, str]]:
    """强制重新拉取角色列表并覆盖缓存，失败时抛出异常。"""
    items = await fetch_characters()
    fetched_at, items = _remember(time.time(), items)
    _write_disk(fetched_at, items)
    return items


def _cached_characters() -> tuple[float, list[dict[str, str]]] | None:
    """返回内存或磁盘中的角色列表，不联网。"""
    cached = cache.get(("wuwa", "characters"))
    if cached is not None:
        return cached
    cached = _read_disk()
    if cached is None:
        return None
    # 只有从磁盘恢复时才重建别名索引，别让每条消息都白算一遍
    return _remember(*cached)


def _expired(cached: tuple[float, list[dict[str, str]]] | None) -> bool:
    """判断缓存是否缺失或过期。"""
    return cached is None or time.time() - cached[0] >= cfg_character_cache_ttl()


_refresh_requested = False


def get_characters() -> list[dict[str, str]]:
    """读取角色列表。

    命令匹配发生在事件处理的并发关键路径上，因此这里只读缓存绝不联网；
    过期时只登记刷新需求，本次仍返回旧数据，避免把接口延迟传导给其他路由。
    """
    global _refresh_requested
    cached = _cached_characters()
    if _expired(cached):
        _refresh_requested = True
    return cached[1] if cached is not None else []


def match_character(items: list[dict[str, str]], name: str) -> dict[str, str] | None:
    """按角色名或 slug 匹配角色，忽略大小写、空白与间隔号。"""
    target = normalize(name)
    if not target:
        return None
    for item in items:
        if normalize(str(item.get("name") or "")) == target:
            return item
    for item in items:
        if normalize(str(item.get("slug") or "")) == target:
            return item
    return None


def lookup_character(name: str) -> dict[str, str] | None:
    """解析用户输入的角色称呼，返回角色条目，未命中返回 None。

    优先级是正式名 / slug 高于别名：这样「心月狐」这类既是正式角色名、又是别人
    别名的词会稳定落到正式角色上，不会被别名表抢走。
    """
    items = get_characters()
    item = match_character(items, name)
    if item is not None:
        return item

    # 正式名没命中才碰别名表，顺带确认来源文件是否更新过，普通角色名零额外开销
    index = _sync_alias_index(items)
    target = index.get(normalize(name))
    if target is None:
        return None
    return match_character(items, target)


async def refresh_if_due() -> None:
    """周期任务：只有命令确实读到了过期缓存才刷新，失败只记录日志。"""
    global _refresh_requested
    if not _refresh_requested:
        return
    _refresh_requested = False
    try:
        await refresh_characters()
    except WuwaEmojiError:
        logger.opt(exception=True).warning("[wuwa_emoji] 后台刷新角色列表失败")


@scheduled(interval=60, name="wuwa_refresh")
async def _refresh_task(context) -> None:
    """由核心调度器负责周期刷新，不创建裸后台任务。"""
    del context
    await refresh_if_due()
