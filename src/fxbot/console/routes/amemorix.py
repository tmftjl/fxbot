"""长期记忆管理的 HTTP 适配：鉴权、参数和响应转换。"""

from __future__ import annotations

from typing import Any
from collections.abc import Awaitable

from fastapi import Depends, Request, APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..auth import sse_auth, bearer_auth
from ..services import memory_admin
from ..dependencies import runtime_for
from ..services.errors import Download, AdminFailure, MemoryAdminError

router = APIRouter(prefix="/amemorix", tags=["fxbot-amemorix"], dependencies=[Depends(bearer_auth)])
file_router = APIRouter(prefix="/amemorix", tags=["fxbot-amemorix"], dependencies=[Depends(sse_auth)])


async def _respond(operation: Awaitable[Any]) -> Any:
    try:
        result = await operation
    except MemoryAdminError as exc:
        status = {
            AdminFailure.INVALID_INPUT: 400,
            AdminFailure.NOT_FOUND: 404,
            AdminFailure.UNAVAILABLE: 503,
            AdminFailure.FAILED: 500,
        }[exc.kind]
        raise HTTPException(status_code=status, detail=exc.detail) from None
    if isinstance(result, Download):
        return FileResponse(result.path, media_type=result.media_type, filename=result.filename)
    return result


@router.get("/status")
async def status(tenant: str) -> dict[str, Any]:
    return await _respond(memory_admin.status(tenant=tenant))


@router.get("/records")
async def search_records(
    tenant: str, query: str = "", types: str = "", limit: int = 50, include_inactive: bool = False
) -> dict[str, Any]:
    return await _respond(
        memory_admin.search_records(
            tenant=tenant, query=query, types=types, limit=limit, include_inactive=include_inactive
        )
    )


@router.get("/records/{record_type}/{record_id}")
async def record_detail(record_type: str, record_id: str, tenant: str) -> dict[str, Any]:
    return await _respond(memory_admin.record_detail(record_type=record_type, record_id=record_id, tenant=tenant))


@router.delete("/records/{record_type}/{record_id}")
async def delete_record(record_type: str, record_id: str, tenant: str) -> dict[str, Any]:
    return await _respond(memory_admin.delete_record(record_type=record_type, record_id=record_id, tenant=tenant))


@router.get("/sources")
async def list_sources(tenant: str) -> dict[str, Any]:
    return await _respond(memory_admin.list_sources(tenant=tenant))


@router.get("/episodes")
async def list_episodes(tenant: str, query: str = "", limit: int = 50) -> dict[str, Any]:
    return await _respond(memory_admin.list_episodes(tenant=tenant, query=query, limit=limit))


@router.get("/profiles")
async def list_profiles(tenant: str, query: str = "", limit: int = 50) -> dict[str, Any]:
    return await _respond(memory_admin.list_profiles(tenant=tenant, query=query, limit=limit))


@router.get("/profiles/{person_id}")
async def profile_detail(person_id: str, tenant: str) -> dict[str, Any]:
    return await _respond(memory_admin.profile_detail(person_id=person_id, tenant=tenant))


@router.get("/graph")
async def get_graph(tenant: str, limit: int = 200) -> dict[str, Any]:
    return await _respond(memory_admin.get_graph(tenant=tenant, limit=limit))


@router.get("/graph/node")
async def graph_node(tenant: str, node: str) -> dict[str, Any]:
    return await _respond(memory_admin.graph_node(tenant=tenant, node=node))


@router.get("/timeline")
async def timeline(tenant: str, limit: int = 100) -> dict[str, Any]:
    return await _respond(memory_admin.timeline(tenant=tenant, limit=limit))


@router.get("/recycle")
async def recycle_bin(tenant: str, limit: int = 50) -> dict[str, Any]:
    return await _respond(memory_admin.recycle_bin(tenant=tenant, limit=limit))


@router.post("/recycle/restore")
async def restore_deleted(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.restore_deleted(payload=payload))


@router.post("/recycle/purge")
async def purge_deleted(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.purge_deleted(payload=payload))


@router.get("/corrections")
async def corrections(tenant: str, kind: str = "", limit: int = 50) -> dict[str, Any]:
    return await _respond(memory_admin.corrections(tenant=tenant, kind=kind, limit=limit))


@router.post("/corrections/preview")
async def preview_correction(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.preview_correction(payload=payload))


@router.post("/corrections/execute")
async def execute_correction(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.execute_correction(payload=payload))


@router.post("/corrections/rollback")
async def rollback_correction(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.rollback_correction(payload=payload))


@router.get("/bundles")
async def list_bundles(limit: int = 50) -> dict[str, Any]:
    return await _respond(memory_admin.list_bundles(limit=limit))


@router.get("/bundles/inspect")
async def inspect_bundle(path: str) -> dict[str, Any]:
    return await _respond(memory_admin.inspect_bundle(path=path))


@router.post("/bundles/export")
async def export_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.export_bundle(payload=payload))


@router.post("/bundles/import")
async def import_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.import_bundle(payload=payload))


@router.post("/bundles/uninstall")
async def uninstall_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.uninstall_bundle(payload=payload))


@file_router.get("/bundles/file")
async def download_bundle(file_name: str) -> FileResponse:
    return await _respond(memory_admin.download_bundle(file_name=file_name))


@router.get("/imports")
async def list_imports(limit: int = 50) -> dict[str, Any]:
    return await _respond(memory_admin.list_imports(limit=limit))


@router.get("/imports/settings")
async def import_settings() -> dict[str, Any]:
    return await _respond(memory_admin.import_settings())


@router.get("/imports/{task_id}")
async def import_task(task_id: str, chunks: bool = False) -> dict[str, Any]:
    return await _respond(memory_admin.import_task(task_id=task_id, chunks=chunks))


@router.post("/imports/paste")
async def paste_import(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.paste_import(payload=payload))


@router.post("/imports/upload")
async def upload_import(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.upload_import(payload=payload))


@router.post("/imports/{task_id}/cancel")
async def cancel_import(task_id: str) -> dict[str, Any]:
    return await _respond(memory_admin.cancel_import(task_id=task_id))


@router.post("/imports/{task_id}/retry")
async def retry_import(task_id: str) -> dict[str, Any]:
    return await _respond(memory_admin.retry_import(task_id=task_id))


@router.get("/images")
async def list_images(tenant: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return await _respond(memory_admin.list_images(tenant=tenant, limit=limit, offset=offset))


@router.get("/images/status")
async def image_status() -> dict[str, Any]:
    return await _respond(memory_admin.image_status())


@router.post("/images/retry")
async def retry_images() -> dict[str, Any]:
    return await _respond(memory_admin.retry_images())


@router.post("/images")
async def upload_image(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.upload_image(payload=payload))


@router.get("/images/{asset_id}/detail")
async def image_detail(asset_id: str, tenant: str, request: Request) -> dict[str, Any]:
    return await _respond(
        memory_admin.image_detail(asset_id=asset_id, tenant=tenant, application=runtime_for(request).application)
    )


@router.delete("/images/{occurrence_id}")
async def delete_image(occurrence_id: str, tenant: str) -> dict[str, Any]:
    return await _respond(memory_admin.delete_image(occurrence_id=occurrence_id, tenant=tenant))


@file_router.get("/images/{occurrence_id}/file")
async def image_file(occurrence_id: str, tenant: str) -> FileResponse:
    return await _respond(memory_admin.image_file(occurrence_id=occurrence_id, tenant=tenant))


@router.get("/runtime")
async def runtime_status() -> dict[str, Any]:
    return await _respond(memory_admin.runtime_status())


@router.post("/runtime/self-check")
async def runtime_self_check() -> dict[str, Any]:
    return await _respond(memory_admin.runtime_self_check())


@router.get("/config")
async def get_config() -> dict[str, Any]:
    return await _respond(memory_admin.get_config())


@router.put("/config")
async def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    return await _respond(memory_admin.update_config(payload=payload))
