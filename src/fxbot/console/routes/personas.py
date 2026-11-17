"""控制台 AI 人格管理；知识库接口在当前版本明确未启用。"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, APIRouter, HTTPException

from ..auth import bearer_auth
from ...chat.personas import (
    DEFAULT_PERSONA,
    list_personas,
    delete_persona,
    reload_personas,
    save_persona_text,
)

router = APIRouter(prefix="/ai_chat", tags=["fxbot-ai-personas"], dependencies=[Depends(bearer_auth)])


@router.get("/personas")
async def get_personas(refresh: bool = False) -> dict[str, Any]:
    """返回人格名称到内容的映射，可选强制重新扫描磁盘。"""
    items = reload_personas() if refresh else list_personas()
    return {"default": DEFAULT_PERSONA, "items": items}


@router.post("/persona")
async def create_persona(payload: dict[str, Any]) -> dict[str, Any]:
    """创建新人格；同名已存在时拒绝覆盖。"""
    key = str(payload.get("key") or "").strip()
    desc = str(payload.get("desc") or "").strip()
    try:
        save_persona_text(key, desc, overwrite=False)
    except FileExistsError:
        raise HTTPException(status_code=400, detail="已存在同名人格") from None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return {"success": True, "message": "人格已创建"}


@router.put("/persona/{key}")
async def update_persona(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """覆盖已有或新建指定名称的人格。"""
    desc = str(payload.get("desc") or "").strip()
    try:
        save_persona_text(key, desc, overwrite=True)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return {"success": True, "message": "人格已保存"}


@router.delete("/persona/{key}")
async def remove_persona(key: str) -> dict[str, Any]:
    """删除人格；默认人格拒绝删除。"""
    try:
        removed = delete_persona(key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    if not removed:
        raise HTTPException(status_code=404, detail="人格不存在")
    return {"success": True, "message": "人格已删除"}


@router.get("/knowledge/{persona}/stats")
async def knowledge_stats(persona: str) -> dict[str, Any]:
    """当前版本未接入人格知识库。"""
    del persona
    raise HTTPException(status_code=501, detail="当前版本未启用知识库功能")


@router.post("/knowledge/{persona}/text")
async def import_knowledge_text(persona: str, payload: dict[str, Any]) -> dict[str, Any]:
    """当前版本未接入人格知识库。"""
    del persona, payload
    raise HTTPException(status_code=501, detail="当前版本未启用知识库功能")


@router.delete("/knowledge/{persona}")
async def clear_knowledge(persona: str) -> dict[str, Any]:
    """当前版本未接入人格知识库。"""
    del persona
    raise HTTPException(status_code=501, detail="当前版本未启用知识库功能")
