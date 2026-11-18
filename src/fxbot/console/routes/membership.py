"""会员控制台路由。"""

from __future__ import annotations

from typing import Any
from datetime import datetime, timezone, timedelta

from loguru import logger
from fastapi import Depends, Request, APIRouter, HTTPException

from fxbot.core.errors import ActionFailed

from ..auth import bearer_auth
from ...core import Message, SessionTarget
from ..dependencies import runtime_for
from ...membership.guard import membership_guard
from ...core.capabilities import Capability
from ...membership.contact import renewal_contact_text, format_membership_expiry
from ...membership.service import MembershipError, membership_service

router = APIRouter(prefix="/membership", tags=["fxbot-membership"], dependencies=[Depends(bearer_auth)])


def _online_group_bot(request: Request, row: Any):
    """裸群号用 managed_by_bot 找回平台与 Bot，规范键继续兼容。"""
    group_id = str(row.group_id).strip()
    target = None
    if ":" in group_id:
        try:
            target = SessionTarget.from_key(group_id)
        except ValueError:
            target = None
        if target is not None and target.kind != "group":
            raise HTTPException(status_code=409, detail="会员群记录不是群会话")
    if target is None:
        manager = str(row.managed_by_bot or "").strip()
        if not manager:
            raise HTTPException(status_code=409, detail="群记录缺少管理 Bot")
        bot_id = manager.rsplit(":", 1)[-1]
        bot = next((item for item in runtime_for(request).application.bots if item.bot_id == bot_id), None)
        if bot is None:
            raise HTTPException(status_code=503, detail="托管 Bot 不在线")
        return bot, SessionTarget(bot.platform, bot.bot_id, "group", group_id)
    bot = next(
        (
            item
            for item in runtime_for(request).application.bots
            if (item.platform, item.bot_id) == (target.platform, target.bot_id)
        ),
        None,
    )
    if bot is None:
        raise HTTPException(status_code=503, detail="托管 Bot 不在线")
    return bot, target


async def _send_group_message(request: Request, row: Any, message: str) -> str:
    """只通过记录所属平台的连接发送，不把失败包装成删除记录的理由。"""
    bot, target = _online_group_bot(request, row)
    await bot.send(target, Message.text(message))
    return target.bot_key()


def _duration_unit(unit: str) -> str:
    """转换控制台时长单位。"""
    value = str(unit).strip().lower()
    if value in {"天", "day", "days", "d"}:
        return "day"
    if value in {"月", "month", "months", "m"}:
        return "month"
    if value in {"年", "year", "years", "y"}:
        return "year"
    raise ValueError(f"不支持的续费单位: {unit}")


def _code_to_console(row: Any) -> dict[str, Any]:
    """转换续费码为控制台数据。"""
    return {
        "code": row.code,
        "length": row.duration_value,
        "unit": {"day": "天", "month": "月", "year": "年"}.get(row.duration_unit, row.duration_unit),
        "max_use": row.max_use,
        "used_count": row.used_count,
        "generated_time": row.created_at.isoformat(),
        "expire_at": row.expires_at.isoformat() if row.expires_at else None,
        "status": row.status,
    }


def _group_to_console(row: Any) -> dict[str, Any]:
    """转换会员群为控制台数据。"""
    return {
        "id": row.id,
        "group_id": row.group_id,
        "expiry": row.expires_at.isoformat() if row.expires_at else "",
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "status": row.status,
        "managed_by_bot": row.managed_by_bot,
        "remark": row.remark,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _parse_console_expiry(payload: dict[str, Any]) -> datetime | None:
    """解析控制台传入的到期时间。"""
    expiry = payload.get("expiry") or payload.get("expires_at")
    if not expiry:
        return None
    text = str(expiry)
    if len(text) == 10:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("/groups")
async def list_groups() -> list[dict[str, Any]]:
    """列出会员群。"""
    rows = await membership_service.list_groups()
    return [_group_to_console(row) for row in rows]


@router.get("/data")
async def get_membership_data() -> dict[str, Any]:
    """返回控制台使用的会员数据快照。"""
    groups = await membership_service.list_groups()
    return {row.group_id: _group_to_console(row) for row in groups}


@router.get("/codes")
async def list_codes() -> dict[str, Any]:
    """列出仍可使用的续费码。"""
    rows = await membership_service.list_codes()
    return {row.code: _code_to_console(row) for row in rows if row.status == "active" and row.used_count < row.max_use}


@router.post("/generate")
async def generate_code_for_console(payload: dict[str, Any]) -> dict[str, Any]:
    """按控制台参数生成续费码。"""
    try:
        expire_days = int(payload.get("expire_days") or 0)
        expires_at = datetime.now(timezone.utc) + timedelta(days=expire_days) if expire_days > 0 else None
        row = await membership_service.generate_code(
            duration_value=int(payload.get("length")),
            duration_unit=_duration_unit(str(payload.get("unit"))),
            max_use=int(payload.get("max_use", 1)),
            expires_at=expires_at,
        )
    except (ValueError, TypeError, MembershipError):
        raise HTTPException(status_code=400, detail="会员参数无效，请核对输入") from None
    return {"code": row.code}


@router.post("/extend")
async def extend_from_console(payload: dict[str, Any]) -> dict[str, Any]:
    """按控制台参数创建或更新会员群。"""
    group_id = str(payload.get("group_id") or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail="群号不能为空")

    try:
        expiry = _parse_console_expiry(payload)
        if expiry is not None:
            row = await membership_service.upsert_group(
                group_id,
                expires_at=expiry,
                status=str(payload.get("status") or "active"),
                managed_by_bot=str(payload.get("managed_by_bot") or "") or None,
                remark=payload.get("remark"),
            )
        else:
            result = await membership_service.extend_group(
                group_id,
                duration_value=int(payload.get("length")),
                duration_unit=_duration_unit(str(payload.get("unit"))),
                operator_user_id=str(payload.get("renewer") or payload.get("renewed_by") or "") or None,
                code="console",
            )
            row = result.group
        await membership_guard.invalidate(group_id)
    except (ValueError, TypeError, MembershipError):
        raise HTTPException(status_code=400, detail="会员参数无效，请核对输入") from None
    return {
        "group_id": row.group_id,
        "expiry": row.expires_at.isoformat() if row.expires_at else "",
        "id": row.id,
    }


@router.post("/remind")
async def remind_group(payload: dict[str, Any], request: Request) -> dict[str, int]:
    """向会员群发送续费提示。"""
    group_id = str(payload.get("group_id") or "").strip()
    row = await membership_service.get_group(group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="群不存在")
    expires = format_membership_expiry(row.expires_at)
    await _send_group_message(request, row, f"本群会员到期时间：{expires}。\n{renewal_contact_text()}")
    return {"sent": 1}


@router.delete("/groups/{group_id}")
async def delete_group_record(group_id: str) -> dict[str, int]:
    """只删除会员记录，不执行退群。"""
    if not await membership_service.delete_group(group_id):
        raise HTTPException(status_code=404, detail="群不存在")
    await membership_guard.invalidate(group_id)
    return {"deleted": 1}


@router.post("/notify")
async def notify_groups(payload: dict[str, Any], request: Request) -> dict[str, int]:
    """批量发送群通知。"""
    group_ids = [str(item).strip() for item in payload.get("group_ids") or [] if str(item).strip()]
    text = str(payload.get("text") or "").strip()
    if not group_ids:
        raise HTTPException(status_code=400, detail="群列表不能为空")
    if not text:
        raise HTTPException(status_code=400, detail="通知文本不能为空")

    sent = 0
    failed = 0
    for group_id in group_ids:
        row = await membership_service.get_group(group_id)
        if row is None:
            failed += 1
            continue
        try:
            await _send_group_message(request, row, text)
            sent += 1
        except (HTTPException, ConnectionError, ActionFailed, TimeoutError):
            logger.opt(exception=True).warning("会员通知发送失败")
            failed += 1
    return {"sent": sent, "failed": failed}


@router.post("/leave")
async def leave_group(payload: dict[str, Any], request: Request) -> dict[str, int]:
    """退出会员群并删除记录。"""
    group_id = str(payload.get("group_id") or "").strip()
    row = await membership_service.get_group(group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="群不存在")
    bot, target = _online_group_bot(request, row)
    await bot.capabilities.require(Capability.GROUP_DIRECTORY).leave_group(target.target_id)
    await membership_service.delete_group(group_id)
    await membership_guard.invalidate(group_id)
    return {"left": 1}


@router.post("/job/run")
async def run_membership_job(request: Request) -> dict[str, int]:
    """手动触发会员任务。"""
    result = await runtime_for(request).membership.process()
    return {"expired": result.expired, "left": result.left}


@router.post("/codes")
async def generate_code(payload: dict[str, Any]) -> dict[str, Any]:
    """生成续费码。"""
    try:
        expires_at = payload.get("expires_at")
        row = await membership_service.generate_code(
            duration_value=int(payload.get("duration_value")),
            duration_unit=str(payload.get("duration_unit")),
            max_use=int(payload.get("max_use", 1)),
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
        )
    except (ValueError, TypeError, MembershipError):
        raise HTTPException(status_code=400, detail="会员参数无效，请核对输入") from None
    return row.model_dump(mode="json")


@router.post("/groups/{group_id}/extend")
async def extend_group(group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """手动延期会员群。"""
    try:
        result = await membership_service.extend_group(
            group_id,
            duration_value=int(payload.get("duration_value")),
            duration_unit=str(payload.get("duration_unit")),
            operator_user_id=str(payload.get("operator_user_id") or "") or None,
            code="console",
        )
        await membership_guard.invalidate(group_id)
    except (ValueError, TypeError, MembershipError):
        raise HTTPException(status_code=400, detail="会员参数无效，请核对输入") from None
    return result.group.model_dump(mode="json")


@router.put("/groups/{group_id}")
async def upsert_group(group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """新增或更新会员群。"""
    expires_at = payload.get("expires_at")
    try:
        row = await membership_service.upsert_group(
            group_id,
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
            status=str(payload.get("status") or "active"),
            managed_by_bot=str(payload.get("managed_by_bot") or "") or None,
            remark=payload.get("remark"),
        )
        await membership_guard.invalidate(group_id)
    except (ValueError, TypeError, MembershipError):
        raise HTTPException(status_code=400, detail="会员配置参数无效") from None
    return row.model_dump(mode="json")
