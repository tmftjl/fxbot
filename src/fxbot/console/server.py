"""接收显式 ASGI 应用与功能路由的控制台装配。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence

from fastapi import FastAPI, APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_console(app: FastAPI, features: Sequence[APIRouter]) -> None:
    """挂载给定功能与前端产物，不查询宿主或自行加载业务。"""
    router = APIRouter(prefix="/fxbot")
    for feature in features:
        router.include_router(feature)

    @router.get("/health")
    async def health() -> dict[str, bool]:
        """公开的控制台健康检查不返回身份和配置。"""
        return {"ok": True}

    assets = Path(__file__).parent / "web/dist"
    index_file = assets / "index.html"

    @router.get("")
    async def index() -> FileResponse:
        """首页不缓存，静态资源使用自身带哈希的文件名。"""
        return FileResponse(index_file, media_type="text/html", headers={"Cache-Control": "no-cache"})

    app.include_router(router)
    app.mount("/fxbot", StaticFiles(directory=assets, html=True), name="fxbot_console")
