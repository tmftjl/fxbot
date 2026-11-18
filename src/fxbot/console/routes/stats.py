"""控制台统计：发送量趋势与模型调用用量。"""

from __future__ import annotations

from fastapi import Depends, Request, APIRouter

from ..auth import bearer_auth
from ..dependencies import runtime_for
from ...chat.providers.usage import get_model_task_usage

router = APIRouter(prefix="/stats", tags=["fxbot-stats"], dependencies=[Depends(bearer_auth)])


@router.get("/today")
async def get_today_stats(request: Request) -> dict[str, object]:
    """返回北京时间今天的发送统计，按 Bot 分组。"""
    return await runtime_for(request).message_stats.today()


@router.get("/history")
async def get_history_stats(request: Request, days: int = 14) -> dict[str, object]:
    """返回最近 N 天的发送趋势，日期按北京时间。"""
    return await runtime_for(request).message_stats.history(days)


@router.get("/model_usage")
async def get_model_usage(hours: int = 24) -> dict[str, object]:
    """返回最近 N 小时的模型调用汇总。"""
    return await get_model_task_usage(int(hours))
