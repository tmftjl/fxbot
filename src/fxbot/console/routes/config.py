"""配置 HTTP 边界；表单声明和插件发现不在路由中执行。"""

from __future__ import annotations

from typing import Any

from loguru import logger
from fastapi import Depends, Request, APIRouter, HTTPException

from ..auth import bearer_auth
from ...config import get_manager
from ..schemas.system import chat_config_tab, system_config_tab
from ...config.manager import ConfigConflictError

router = APIRouter(prefix="/config", tags=["fxbot-config"], dependencies=[Depends(bearer_auth)])


def _plugin_config_tabs(request: Request) -> list[dict[str, Any]]:
    tabs = []
    for extensions in request.app.state.runtime.plugin_extensions.values():
        factory = extensions.get("console.config")
        if factory is None:
            continue
        schema = factory()
        if not isinstance(schema, dict):
            raise TypeError("插件配置界面必须返回对象")
        tabs.append(schema)
    return tabs


def _tab_sort_key(tab: dict[str, Any]) -> tuple[int, str]:
    """按 schema 声明的顺序排列配置页。"""
    try:
        order = int(tab.get("order", 1000))
    except (TypeError, ValueError):
        order = 1000
    return order, str(tab.get("key") or "")


@router.get("/tabs")
async def get_config_tabs(request: Request) -> list[dict[str, Any]]:
    """返回控制台配置表单结构。"""
    return sorted([system_config_tab(), *_plugin_config_tabs(request)], key=_tab_sort_key)


@router.get("/chat")
async def getchat_config_tab() -> dict[str, Any]:
    """返回 AI 对话设置页的表单结构。"""
    return chat_config_tab()


@router.get("")
async def get_config() -> dict[str, Any]:
    """读取控制台配置。"""
    return get_manager().get_console_configs()


@router.put("")
async def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    """保存控制台配置。"""
    manager = get_manager()
    try:
        base_revisions = payload.pop("base_revisions", None)
        manager.save_console_configs(payload, base_revisions=base_revisions)
    except ConfigConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.opt(exception=True).error("[Console] 保存配置失败")
        raise HTTPException(status_code=500, detail="保存失败，请稍后再试") from exc
    # 回传保存后的修订号，客户端可以直接基于自己的快照继续提交，不必先重新拉一次配置
    return {"success": True, "revisions": manager.revisions(payload)}
