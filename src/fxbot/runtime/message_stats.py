"""按 Bot 和会话目标统计成功发送的消息，兼容旧版控制台缓存。"""

from __future__ import annotations

import os
import json
import asyncio
from copy import deepcopy
from typing import Any
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict

from loguru import logger

from ..utils.tz import today_str
from ..core.types import BotClient, SessionTarget
from ..utils.paths import data_dir
from ..core.application import Application

CACHE_FILE = data_dir("system") / "status_message_cache.json"
HISTORY_DAYS = 30
FLUSH_INTERVAL_SECONDS = 60


def _new_entry() -> dict[str, Any]:
    """新建单个 Bot 的统计条目，发送方向按目标拆分。"""
    return {
        "received": 0,
        "sent": 0,
        "group": {"count": 0, "targets": {}},
        "private": {"count": 0, "targets": {}},
    }


def _normalize_entry(raw: Any) -> dict[str, Any]:
    """把旧版或损坏的条目补齐成当前结构。"""
    entry = _new_entry()
    if not isinstance(raw, dict):
        return entry
    entry["received"] = int(raw.get("received") or 0)
    entry["sent"] = int(raw.get("sent") or 0)
    for chat_type in ("group", "private"):
        bucket = raw.get(chat_type)
        if not isinstance(bucket, dict):
            continue
        targets = bucket.get("targets")
        if isinstance(targets, dict):
            entry[chat_type]["targets"] = {str(key): int(value or 0) for key, value in targets.items()}
        entry[chat_type]["count"] = int(bucket.get("count") or sum(entry[chat_type]["targets"].values()))
    return entry


def _archive_totals(target: dict[str, dict[str, int]], date_text: str, bots: dict[str, dict[str, Any]]) -> None:
    """把某天的 Bot 明细汇总进历史，并滚动保留固定天数。"""
    sent = group = private = 0
    for entry in bots.values():
        sent += int(entry.get("sent") or 0)
        group += int((entry.get("group") or {}).get("count") or 0)
        private += int((entry.get("private") or {}).get("count") or 0)
    if sent <= 0:
        return
    target[date_text] = {"sent": sent, "group": group, "private": private}
    for old in sorted(target)[:-HISTORY_DAYS]:
        target.pop(old, None)


class MessageStats:
    """进程内累计发送统计，定期写入与旧版相同的 JSON 文件。"""

    def __init__(self, path: Path | None = None) -> None:
        """绑定缓存文件；测试可传入独立路径。"""
        self.path = path or CACHE_FILE
        self._bots: dict[str, dict[str, Any]] = defaultdict(_new_entry)
        self._history: dict[str, dict[str, int]] = {}
        self._date = today_str()
        self._dirty = False
        self._loaded = False
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    def load(self) -> None:
        """读取旧版缓存；跨天明细先归档，损坏或缺失时从空统计开始。"""
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.opt(exception=True).error("[MessageStats] 读取消息统计缓存失败")
            return
        if not isinstance(data, dict):
            logger.error("[MessageStats] 消息统计缓存必须是对象")
            return
        cache_date = str(data.get("date") or "")
        raw_bots = data.get("bots")
        raw_history = data.get("history")
        bots = (
            {str(key): _normalize_entry(value) for key, value in raw_bots.items()} if isinstance(raw_bots, dict) else {}
        )
        history = (
            {
                str(day): {
                    "sent": int(totals.get("sent") or 0),
                    "group": int(totals.get("group") or 0),
                    "private": int(totals.get("private") or 0),
                }
                for day, totals in raw_history.items()
                if isinstance(totals, dict)
            }
            if isinstance(raw_history, dict)
            else {}
        )
        self._bots.clear()
        self._history = history
        if cache_date and cache_date != today_str() and bots:
            _archive_totals(self._history, cache_date, bots)
        if cache_date == today_str() and bots:
            self._bots.update(bots)
        self._date = today_str()
        self._dirty = True
        logger.info(
            "[MessageStats] 已加载消息统计缓存: {} 个 Bot, {} 天历史",
            len(self._bots),
            len(self._history),
        )

    async def start(self, application: Application) -> None:
        """加载缓存并启动周期落盘。"""
        self.load()
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = application.spawn(self._flush_loop(), name="message-stats")

    async def stop(self) -> None:
        """停止周期落盘并保存最后一份快照。"""
        task, self._flush_task = self._flush_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.flush()

    async def record_send(self, bot: BotClient, target: SessionTarget) -> None:
        """记录一次成功发送；频道和未知会话不进入群聊/私聊统计。"""
        if target.kind not in {"group", "private"}:
            return
        try:
            async with self._lock:
                self._ensure_today_locked()
                entry = self._bots[f"bot_{bot.bot_id}"]
                entry["sent"] += 1
                bucket = entry[target.kind]
                bucket["count"] += 1
                bucket["targets"][target.target_id] = bucket["targets"].get(target.target_id, 0) + 1
                self._dirty = True
        except Exception:
            logger.opt(exception=True).error("[MessageStats] 记录发送统计失败")

    async def today(self) -> dict[str, Any]:
        """返回今日发送快照，形状与旧版控制台一致。"""
        async with self._lock:
            self._ensure_today_locked()
            bots: dict[str, Any] = {}
            for cache_key, entry in self._bots.items():
                stats: dict[str, Any] = {"total_sent": int(entry.get("sent") or 0)}
                for chat_type in ("group", "private"):
                    bucket = entry.get(chat_type) or {}
                    count = int(bucket.get("count") or 0)
                    if count > 0:
                        stats[chat_type] = {"count": count, "targets": dict(bucket.get("targets") or {})}
                bots[cache_key.removeprefix("bot_")] = stats
            return {"bots": bots}

    async def history(self, days: int = 14) -> dict[str, Any]:
        """返回最近若干天的发送汇总，缺失日期补零。"""
        window = max(1, min(int(days), HISTORY_DAYS))
        async with self._lock:
            self._ensure_today_locked()
            _archive_totals(self._history, self._date, self._bots)
            history = {day: dict(totals) for day, totals in self._history.items()}
        end = date.fromisoformat(today_str())
        items = []
        for offset in range(window - 1, -1, -1):
            day = (end - timedelta(days=offset)).isoformat()
            totals = history.get(day) or {}
            items.append(
                {
                    "date": day,
                    "sent": int(totals.get("sent") or 0),
                    "group": int(totals.get("group") or 0),
                    "private": int(totals.get("private") or 0),
                }
            )
        return {"days": len(items), "items": items}

    async def flush(self) -> None:
        """有变更时原子写入缓存文件；写失败保留脏标记等待下次重试。"""
        if not self._dirty:
            return
        async with self._lock:
            if not self._dirty:
                return
            self._ensure_today_locked()
            _archive_totals(self._history, self._date, self._bots)
            payload = {
                "date": self._date,
                "bots": deepcopy(dict(self._bots)),
                "history": {day: dict(totals) for day, totals in self._history.items()},
            }
            self._dirty = False
        tmp_file = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_file, self.path)
        except Exception:
            self._dirty = True
            tmp_file.unlink(missing_ok=True)
            logger.opt(exception=True).error("[MessageStats] 保存消息统计缓存失败")

    async def _flush_loop(self) -> None:
        """按固定间隔落盘，进程停止时由 stop 再保存一次。"""
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await self.flush()

    def _ensure_today_locked(self) -> None:
        """跨天时先把旧明细归档，调用方必须持有锁。"""
        current = today_str()
        if self._date == current:
            return
        _archive_totals(self._history, self._date, self._bots)
        self._bots.clear()
        self._date = current
        self._dirty = True
