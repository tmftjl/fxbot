"""在线 Bot 状态路由。"""

from __future__ import annotations

from fastapi import Depends, Request, APIRouter

from ..auth import bearer_auth

router = APIRouter(prefix="/bots", tags=["fxbot-bots"], dependencies=[Depends(bearer_auth)])


@router.get("")
async def list_bots(request: Request) -> dict[str, list[dict[str, str]]]:
    """列出在线 Bot。"""
    bots = []
    for bot in request.app.state.runtime.bots:
        bots.append({"self_id": bot.bot_id, "adapter": bot.platform})
    return {"bots": bots}
