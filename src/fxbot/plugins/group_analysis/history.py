"""群历史消息拉取、统计与分析输入。"""

from __future__ import annotations

import re
from typing import Any
from datetime import datetime, timedelta
from dataclasses import dataclass

from ...core import BotClient, SessionTarget, HistoryMessage
from ...utils.tz import SHANGHAI_TZ
from ...core.capabilities import Capability


@dataclass(slots=True)
class HistoryWindow:
    """分析窗口与展示标签。"""

    start: int
    end: int
    label: str
    days: int


def day_window(days: int, *, end: int, label: str) -> HistoryWindow:
    return HistoryWindow(start=end - days * 86400, end=end, label=label, days=days)


async def collect_messages(
    bot: BotClient,
    target: SessionTarget,
    window: HistoryWindow,
    *,
    limit: int,
    filter_self: bool,
) -> list[HistoryMessage]:
    """拉取窗口内消息；机器人自己的发言可选排除。"""
    messages = await bot.capabilities.require(Capability.HISTORY).fetch_group_history(
        target, start_time=window.start, end_time=window.end, limit=limit
    )
    if filter_self:
        messages = [message for message in messages if message.user_id != bot.bot_id]
    return [message for message in messages if window.start <= message.timestamp < window.end]


def _chronological_hours(messages: list[HistoryMessage], window: HistoryWindow) -> tuple[list[datetime], list[int]]:
    """按窗口内的真实时间顺序分桶，避免滚动 24 小时首尾同小时被合并。"""
    cursor = datetime.fromtimestamp(window.start, SHANGHAI_TZ).replace(minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(window.end, SHANGHAI_TZ)
    slots: list[datetime] = []
    while cursor < end:
        slots.append(cursor)
        cursor += timedelta(hours=1)
    counts = [0] * len(slots)
    for message in messages:
        stamp = datetime.fromtimestamp(message.timestamp, SHANGHAI_TZ).replace(minute=0, second=0, microsecond=0)
        index = int((stamp - slots[0]).total_seconds() // 3600) if slots else -1
        if 0 <= index < len(counts):
            counts[index] += 1
    return slots, counts


def _slot_label(slots: list[datetime], index: int | None, end: int) -> str:
    """峰值时间段带上“昨天/今天”，滚动窗口里不会分不清是哪一天的同一小时。"""
    if index is None or not slots:
        return ""
    start = slots[index]
    finish = start + timedelta(hours=1)
    end_date = datetime.fromtimestamp(end, SHANGHAI_TZ).date()
    days = (end_date - start.date()).days
    prefix = "今天" if days == 0 else "昨天" if days == 1 else start.strftime("%m-%d")
    return f"{prefix} {start:%H:%M}-{finish:%H:%M}"


def _range_label(window: HistoryWindow) -> str:
    start = datetime.fromtimestamp(window.start, SHANGHAI_TZ)
    end = datetime.fromtimestamp(window.end, SHANGHAI_TZ)
    if window.days <= 1:
        return f"{start:%m-%d %H:%M} – {end:%m-%d %H:%M}"
    return f"最近 {window.days} 天（截至 {end:%m-%d %H:%M}）"


def build_stats(messages: list[HistoryMessage], window: HistoryWindow | None = None) -> dict[str, Any]:
    """本地统计不依赖模型，模型失败时仍能出概览。"""
    speakers: dict[str, dict[str, Any]] = {}
    images = emojis = characters = 0
    for message in messages:
        images += message.images
        emojis += message.text.count("[表情]")
        characters += len(message.text.replace("[图片]", "").replace("[表情]", "").strip())
        name = message.user_name or message.user_id or "未知成员"
        speaker = speakers.setdefault(message.user_id or name, {"name": name, "count": 0})
        speaker["count"] += 1
    ranked = sorted(speakers.values(), key=lambda item: item["count"], reverse=True)
    peak_label = ""
    if window is not None and window.days <= 1:
        slots, hours = _chronological_hours(messages, window)
        labels = [slot.strftime("%H") for slot in slots]
        peak_hour = max(range(len(hours)), key=lambda index: hours[index]) if any(hours) else None
        peak_label = _slot_label(slots, peak_hour, window.end)
        range_label = _range_label(window)
    else:
        hours = [0] * 24
        for message in messages:
            if 0 <= (hour := datetime.fromtimestamp(message.timestamp, SHANGHAI_TZ).hour) < 24:
                hours[hour] += 1
        labels = [f"{hour:02d}" for hour in range(24)]
        peak_hour = max(range(24), key=lambda hour: hours[hour]) if any(hours) else None
        if peak_hour is not None:
            peak_label = f"{peak_hour:02d}:00-{peak_hour + 1:02d}:00"
        range_label = _range_label(window) if window is not None else ""
    return {
        "total": len(messages),
        "participants": len(speakers),
        "images": images,
        "emojis": emojis,
        "characters": characters,
        "top_speakers": ranked[:5],
        "hours": hours,
        "hour_labels": labels,
        "peak_hour": peak_hour,
        "peak_label": peak_label,
        "range_label": range_label,
    }


def build_analysis_messages(messages: list[HistoryMessage], bot_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """按原版规则生成分析输入：排除机器人/命令，补引用关系。"""
    excluded = {str(item) for item in (bot_ids or set()) if str(item)}
    referenced_ids = {message.reply_to_id for message in messages if message.reply_to_id}
    result: list[dict[str, Any]] = []
    for message in messages:
        if message.user_id in excluded:
            continue
        text = " ".join(message.text.split())
        if text.startswith("/"):
            continue
        if message.reply_to_id:
            text = f"[回复:{message.reply_to_id}] {text}"
        text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text).strip()
        if not text:
            continue
        if message.message_id in referenced_ids:
            text = f"[消息:{message.message_id}] {text}"
        result.append(
            {
                "time": datetime.fromtimestamp(message.timestamp, SHANGHAI_TZ).strftime("%H:%M"),
                "sender": message.user_name or message.user_id,
                "content": text,
                "user_id": message.user_id,
            }
        )
    return result


def build_user_stats(messages: list[HistoryMessage], limit: int = 20) -> list[dict[str, Any]]:
    """按成员聚合发言量、字数、表情、时段与回复数，供称号分析使用。"""
    users: dict[str, dict[str, Any]] = {}
    for message in messages:
        key = message.user_id or message.user_name or "未知成员"
        user = users.setdefault(
            key,
            {
                "user_id": message.user_id,
                "name": message.user_name or message.user_id or "未知成员",
                "count": 0,
                "chars": 0,
                "emojis": 0,
                "hours": {},
                "replies": 0,
            },
        )
        if message.user_name:
            user["name"] = message.user_name
        user["count"] += 1
        user["chars"] += len(message.text.replace("[图片]", "").replace("[表情]", "").strip())
        user["emojis"] += message.text.count("[表情]")
        if message.reply_to_id:
            user["replies"] += 1
        hour = datetime.fromtimestamp(message.timestamp, SHANGHAI_TZ).hour
        user["hours"][hour] = user["hours"].get(hour, 0) + 1
    ranked = sorted(users.values(), key=lambda item: item["count"], reverse=True)
    summaries: list[dict[str, Any]] = []
    for user in ranked[: max(1, limit)]:
        count = user["count"]
        night = sum(value for hour, value in user["hours"].items() if int(hour) < 6)
        summaries.append(
            {
                "name": user["name"],
                "user_id": user["user_id"],
                "message_count": count,
                "avg_chars": round(user["chars"] / count, 1),
                "emoji_ratio": round(user["emojis"] / count, 2),
                "night_ratio": round(night / count, 2),
                "reply_ratio": round(user["replies"] / count, 2),
            }
        )
    return summaries
