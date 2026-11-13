"""群管理工具只使用本轮真实事件，工具参数不能伪造操作者或群。"""

from __future__ import annotations

from .service import GroupAdminService
from ...chat.tools import ToolError, ToolContext, ToolRuntime, tool
from ...chat.session_hooks import session_target


async def _execute(ctx: ToolContext, rt: ToolRuntime, action: str, **arguments) -> dict:
    event = rt.require_event()
    if (
        rt.application is None
        or rt.authorizer is None
        or not event.is_group
        or ctx.user_id != event.user_id
        or ctx.group_id != event.target.target_id
        or not ctx.session_id
        or session_target(ctx.session_id) != event.target
    ):
        raise ToolError("群管理工具必须来自当前真实群会话")
    result = await GroupAdminService(rt.application, rt.authorizer).execute(
        rt.require_bot(), event, action, **arguments
    )
    return {"success": result.success, "message": result.message}


@tool(
    name="mute_member",
    title="禁言成员",
    description="禁言当前群成员。",
    permission="group_admin:mute_member",
    deferred=True,
    parameters={
        "type": "object",
        "properties": {"user_id": {"type": "string"}, "duration": {"type": "integer"}},
        "required": ["user_id", "duration"],
    },
)
async def mute_member_tool(ctx: ToolContext, rt: ToolRuntime, user_id: str, duration: int) -> dict:
    return await _execute(ctx, rt, "mute_member", user_id=user_id, duration=duration)


@tool(
    name="kick_member",
    title="踢出成员",
    description="踢出当前群成员。",
    permission="group_admin:kick_member",
    deferred=True,
    parameters={"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
)
async def kick_member_tool(ctx: ToolContext, rt: ToolRuntime, user_id: str) -> dict:
    return await _execute(ctx, rt, "kick_member", user_id=user_id)


@tool(
    name="unmute_member",
    title="解除禁言",
    description="解除当前群成员禁言。",
    permission="group_admin:unmute_member",
    deferred=True,
    parameters={"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
)
async def unmute_member_tool(ctx: ToolContext, rt: ToolRuntime, user_id: str) -> dict:
    return await _execute(ctx, rt, "unmute_member", user_id=user_id)


@tool(
    name="self_mute",
    title="禁言自己",
    description="禁言当前发起请求的用户。",
    permission="group_admin:self_mute",
    deferred=True,
    parameters={"type": "object", "properties": {"duration_seconds": {"type": "integer"}}},
)
async def self_mute_tool(ctx: ToolContext, rt: ToolRuntime, duration_seconds: int = 600) -> dict:
    return await _execute(ctx, rt, "self_mute", duration=duration_seconds)


@tool(
    name="apply_title",
    title="申请群头衔",
    description="为当前请求用户申请群头衔。",
    permission="group_admin:apply_title",
    deferred=True,
    parameters={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
)
async def apply_title_tool(ctx: ToolContext, rt: ToolRuntime, title: str) -> dict:
    return await _execute(ctx, rt, "apply_title", title=title)
