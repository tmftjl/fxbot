"""群管理原生命令参数和动作分发。"""

from __future__ import annotations

import re

from ...core import Capability, HandlerContext, UnsupportedCapability
from .service import GroupAdminService
from ...core.errors import ActionFailed
from ...core.plugin import on_regex


def parse_duration(text: str, default: int = 600) -> int:
    if not text.strip():
        return default
    match = re.fullmatch(r"\s*(\d+)\s*(秒|s|分钟|分|m|小时|时|h|天|d)?\s*", text, re.I)
    if not match:
        raise ValueError("请填写明确的时长，例如 60秒、10分钟")
    units = {
        "秒": 1,
        "s": 1,
        "分": 60,
        "分钟": 60,
        "m": 60,
        "时": 3600,
        "小时": 3600,
        "h": 3600,
        "天": 86400,
        "d": 86400,
    }
    return max(1, min(2592000, int(match.group(1)) * units[(match.group(2) or "秒").lower()]))


def targets(context: HandlerContext, argument: str) -> list[str]:
    mentions = (context.event.original_message or context.event.message).mentions()
    return list(dict.fromkeys(mentions or re.findall(r"(?<!\d)\d{5,}(?!\d)", argument)))


_DEFINITIONS = (
    ("mute_member", r"^[#＃]禁言(?!列表)\s*(.*)", "admin"),
    ("unmute_member", r"^[#＃]解禁\s*(.*)", "admin"),
    ("ban_kick_member", r"^[#＃]拉黑踢\s*(.*)", "admin"),
    ("kick_member", r"^[#＃]踢\s*(.*)", "admin"),
    ("set_title", r"^[#＃]设置头衔\s*(.*)", "superuser"),
    ("mute_all_on", r"^[#＃]全(?:体|员)禁言", "admin"),
    ("mute_all_off", r"^[#＃]全(?:体|员)解禁", "admin"),
    ("mute_list", r"^[#＃](?:获取|查看)?禁言列表", "admin"),
    ("unmute_all", r"^[#＃]解除全部禁言", "admin"),
    ("self_mute", r"^[#＃]?我要(?:自闭|禅定)\s*(.*)", "all"),
    ("apply_title", r"^[#＃]申请头衔\s*(.*)", "all"),
    ("remove_title", r"^[#＃](?:删除|取消)头衔\s*(.*)", "admin"),
    ("set_admin", r"^[#＃]设置管理\s*(.*)", "owner"),
    ("unset_admin", r"^[#＃]取消管理\s*(.*)", "owner"),
    ("recall_msg", r"^[#＃]撤回", "admin"),
    ("set_essence", r"^[#＃](?:设置精华|设精)", "admin"),
    ("unset_essence", r"^[#＃]取消精华", "admin"),
)


class GroupCommands:
    def __init__(self, service: GroupAdminService) -> None:
        self.service = service

    async def handle(self, action: str, context: HandlerContext) -> None:
        definition = next(item for item in _DEFINITIONS if item[0] == action)
        match = re.search(definition[1], context.arguments)
        argument = match.group(1).strip() if match.lastindex else ""
        users = targets(context, argument)
        bot, event = context.bot, context.event
        if action in {"mute_list", "unmute_all"}:
            if Capability.MUTED_MEMBERS not in bot.capabilities:
                await context.finish("当前实现不支持禁言列表")
            try:
                members = await bot.capabilities.require(Capability.MUTED_MEMBERS).get_muted_members(
                    event.target.target_id
                )
            except UnsupportedCapability:
                await context.finish("当前实现不支持禁言列表")
            except (ActionFailed, TimeoutError, OSError):
                await context.finish("禁言列表查询失败，请稍后重试")
            if action == "mute_list":
                await context.finish(
                    "\n".join(
                        [
                            f"禁言列表（{len(members)} 人）",
                            *[
                                f"{member['nickname']} ({member['user_id']})：剩余 {member['remaining']} 秒"
                                for member in members[:20]
                            ],
                        ]
                    )
                )
            success = 0
            for member in members:
                result = await self.service.execute(bot, event, "unmute_member", user_id=member["user_id"])
                success += int(result.success)
            await context.finish(f"解除全部禁言：成功 {success} 人，失败 {len(members) - success} 人")
        if action in {"self_mute", "apply_title", "remove_title"} and not users:
            users = [event.user_id]
        if action in {"mute_all_on", "mute_all_off"}:
            users = [""]
        message_id = ""
        if action in {"recall_msg", "set_essence", "unset_essence"}:
            if event.reply is None:
                await context.finish("请引用需要操作的消息")
            reply = await bot.capabilities.require(Capability.REPLY).get_reply(event)
            if reply is None or reply.sender_id is None:
                await context.finish("无法确认引用消息身份，未执行")
            users = [reply.sender_id]
            message_id = reply.message_id
        if not users:
            await context.finish("请 @ 目标成员或提供用户 ID")
        duration = 600
        if action in {"mute_member", "self_mute"}:
            duration_text = argument
            for user in users:
                duration_text = re.sub(rf"(?<!\d){re.escape(user)}(?!\d)", "", duration_text)
            try:
                duration = parse_duration(duration_text.strip(), 60 if action == "mute_member" else 600)
            except ValueError as exc:
                await context.finish(str(exc))
        title = argument
        if action in {"set_title", "remove_title"}:
            for user in users:
                title = re.sub(rf"(?<!\d){re.escape(user)}(?!\d)", "", title).strip()
        results = [
            await self.service.execute(
                bot,
                event,
                action,
                user_id=user,
                duration=duration,
                title=title if action in {"set_title", "apply_title"} else "",
                message_id=message_id,
            )
            for user in users
        ]
        await context.finish(
            "\n".join(
                f"{'✅' if result.success else '❌'} {user}: {result.message}"
                for user, result in zip(users, results, strict=True)
            )
        )


mute_member_matcher = on_regex(
    r"^[#＃]禁言(?!列表)\s*(.*)", name="mute_member", display_name="禁言成员", level="admin", scene="group"
)


@mute_member_matcher.handle()
async def mute_member(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("mute_member", context)


unmute_member_matcher = on_regex(
    r"^[#＃]解禁\s*(.*)", name="unmute_member", display_name="解除禁言", level="admin", scene="group"
)


@unmute_member_matcher.handle()
async def unmute_member(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("unmute_member", context)


ban_kick_member_matcher = on_regex(
    r"^[#＃]拉黑踢\s*(.*)", name="ban_kick_member", display_name="拉黑踢出", level="admin", scene="group"
)


@ban_kick_member_matcher.handle()
async def ban_kick_member(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("ban_kick_member", context)


kick_member_matcher = on_regex(
    r"^[#＃]踢\s*(.*)", name="kick_member", display_name="踢出成员", level="admin", scene="group"
)


@kick_member_matcher.handle()
async def kick_member(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("kick_member", context)


set_title_matcher = on_regex(
    r"^[#＃]设置头衔\s*(.*)", name="set_title", display_name="设置头衔", level="superuser", scene="group"
)


@set_title_matcher.handle()
async def set_title(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("set_title", context)


mute_all_on_matcher = on_regex(
    r"^[#＃]全(?:体|员)禁言", name="mute_all_on", display_name="全员禁言", level="admin", scene="group"
)


@mute_all_on_matcher.handle()
async def mute_all_on(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("mute_all_on", context)


mute_all_off_matcher = on_regex(
    r"^[#＃]全(?:体|员)解禁", name="mute_all_off", display_name="关闭全员禁言", level="admin", scene="group"
)


@mute_all_off_matcher.handle()
async def mute_all_off(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("mute_all_off", context)


mute_list_matcher = on_regex(
    r"^[#＃](?:获取|查看)?禁言列表", name="mute_list", display_name="禁言列表", level="admin", scene="group"
)


@mute_list_matcher.handle()
async def mute_list(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("mute_list", context)


unmute_all_matcher = on_regex(
    r"^[#＃]解除全部禁言", name="unmute_all", display_name="解除全部禁言", level="admin", scene="group"
)


@unmute_all_matcher.handle()
async def unmute_all(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("unmute_all", context)


self_mute_matcher = on_regex(
    r"^[#＃]?我要(?:自闭|禅定)\s*(.*)", name="self_mute", display_name="自助禁言", level="all", scene="group"
)


@self_mute_matcher.handle()
async def self_mute(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("self_mute", context)


apply_title_matcher = on_regex(
    r"^[#＃]申请头衔\s*(.*)", name="apply_title", display_name="申请头衔", level="all", scene="group"
)


@apply_title_matcher.handle()
async def apply_title(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("apply_title", context)


remove_title_matcher = on_regex(
    r"^[#＃](?:删除|取消)头衔\s*(.*)", name="remove_title", display_name="删除头衔", level="admin", scene="group"
)


@remove_title_matcher.handle()
async def remove_title(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("remove_title", context)


set_admin_matcher = on_regex(
    r"^[#＃]设置管理\s*(.*)", name="set_admin", display_name="设置管理员", level="owner", scene="group"
)


@set_admin_matcher.handle()
async def set_admin(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("set_admin", context)


unset_admin_matcher = on_regex(
    r"^[#＃]取消管理\s*(.*)", name="unset_admin", display_name="取消管理员", level="owner", scene="group"
)


@unset_admin_matcher.handle()
async def unset_admin(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("unset_admin", context)


recall_msg_matcher = on_regex(r"^[#＃]撤回", name="recall_msg", display_name="撤回消息", level="admin", scene="group")


@recall_msg_matcher.handle()
async def recall_msg(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("recall_msg", context)


set_essence_matcher = on_regex(
    r"^[#＃](?:设置精华|设精)", name="set_essence", display_name="设置精华", level="admin", scene="group"
)


@set_essence_matcher.handle()
async def set_essence(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("set_essence", context)


unset_essence_matcher = on_regex(
    r"^[#＃]取消精华", name="unset_essence", display_name="取消精华", level="admin", scene="group"
)


@unset_essence_matcher.handle()
async def unset_essence(context: HandlerContext) -> None:
    await moderation(context.app_services).handle("unset_essence", context)


def moderation(runtime):
    return GroupCommands(GroupAdminService(runtime.application, runtime.authorizer))
