"""当前应用加载的插件及命令展示信息。"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import Depends, Request, APIRouter

from ..auth import bearer_auth

router = APIRouter(tags=["fxbot-meta"], dependencies=[Depends(bearer_auth)])


@router.get("/plugins")
async def get_plugins(request: Request) -> dict[str, str]:
    return {name: metadata.name for name, metadata in request.app.state.runtime.plugin_metadata.items()}


@router.get("/plugins/details")
async def get_plugin_details(request: Request) -> dict:
    return {name: asdict(metadata) for name, metadata in request.app.state.runtime.plugin_metadata.items()}


@router.get("/commands")
async def get_commands(request: Request) -> dict[str, dict[str, str]]:
    return request.app.state.runtime.command_metadata
