"""控制台统计：发送量趋势与模型调用用量。"""

from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, APIRouter

from ..auth import bearer_auth
from ...utils.tz import today
from ...chat.providers.usage import get_model_task_usage
from ...chat.memory.repository import chat_repository

router = APIRouter(prefix="/stats", tags=["fxbot-stats"], dependencies=[Depends(bearer_auth)])


def _tenant_parts(tenant: str) -> tuple[str, str, str]:
    """从完整会话键中取出 Bot 标识、会话类型和目标。"""
    parts = tenant.split(":")
    if len(parts) < 4:
        return tenant, "", ""
    return ":".join(parts[:2]), parts[-2], parts[-1]


def _empty_bot() -> dict[str, object]:
    return {
        "total_sent": 0,
        "group": {"count": 0, "targets": {}},
        "private": {"count": 0, "targets": {}},
    }


def _add(bot: dict[str, object], kind: str, target: str, count: int) -> None:
    bot["total_sent"] = int(bot["total_sent"]) + count
    if kind not in {"group", "private"}:
        return
    section = bot[kind]
    assert isinstance(section, dict)
    section["count"] = int(section["count"]) + count
    targets = section["targets"]
    assert isinstance(targets, dict)
    targets[target] = int(targets.get(target, 0)) + count


@router.get("/today")
async def get_today_stats() -> dict[str, object]:
    """返回北京时间今天的发送统计，按 Bot 分组。"""
    bots: dict[str, dict[str, object]] = {}
    for tenant, count in await chat_repository.sent_stats_today():
        bot_key, kind, target = _tenant_parts(tenant)
        _add(bots.setdefault(bot_key, _empty_bot()), kind, target, count)
    return {"bots": bots}


@router.get("/history")
async def get_history_stats(days: int = 14) -> dict[str, object]:
    """返回最近 N 天的发送趋势，日期按北京时间。"""
    window = max(1, min(int(days), 90))
    buckets: dict[str, dict[str, int]] = {}
    for date, tenant, count in await chat_repository.sent_stats_history(days=window):
        _, kind, _ = _tenant_parts(tenant)
        bucket = buckets.setdefault(date, {"sent": 0, "group": 0, "private": 0})
        bucket["sent"] += count
        if kind in {"group", "private"}:
            bucket[kind] += count
    start = today() - timedelta(days=window - 1)
    items = []
    for offset in range(window):
        date = (start + timedelta(days=offset)).isoformat()
        bucket = buckets.get(date, {"sent": 0, "group": 0, "private": 0})
        items.append({"date": date, **bucket})
    return {"days": len(items), "items": items}


@router.get("/model_usage")
async def get_model_usage(hours: int = 24) -> dict[str, object]:
    """返回最近 N 小时的模型调用汇总。"""
    return await get_model_task_usage(int(hours))
