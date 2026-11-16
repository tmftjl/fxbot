"""群管理动作的真实身份校验与单次执行。"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from fxbot.core.errors import ActionFailed

from ...core import Event, BotClient, Capability, SessionTarget, UnsupportedCapability
from ...core.application import Application
from ...permission.authorization import Authorizer


@dataclass(frozen=True)
class ServiceResult:
    success: bool
    message: str


class Rejected(ValueError):
    """可以直接回复操作者的明确拒绝原因。"""


class GroupAdminService:
    def __init__(self, application: Application, authorizer: Authorizer) -> None:
        self.application = application
        self.authorizer = authorizer

    def protected(self, target: SessionTarget, user_id: str) -> bool:
        actor = SessionTarget(target.platform, target.bot_id, "private", user_id).key()
        system = self.authorizer.configuration.get_system()["system"]
        return actor in system["superusers"] or actor in system["bot_admins"]

    def _online(self, bot: BotClient, target: SessionTarget) -> None:
        if target.kind != "group" or (target.platform, target.bot_id) != (bot.platform, bot.bot_id):
            raise Rejected("只能在当前 Bot 的群聊中使用")
        if not self.application.is_registered(bot):
            raise Rejected("当前连接已失效")
        if Capability.MEMBER_MODERATION not in bot.capabilities:
            raise UnsupportedCapability("当前平台不支持群管理")

    async def _roles(self, bot: BotClient, event: Event, user_id: str, self_action: bool) -> tuple[str, str]:
        self._online(bot, event.target)
        own = await bot.capabilities.require(Capability.GROUP_ROLE).member_role(event.target.target_id, bot.bot_id)
        if own not in {"owner", "admin"}:
            raise Rejected("Bot 没有群管理权限")
        operator = await bot.capabilities.require(Capability.GROUP_ROLE).member_role(
            event.target.target_id, event.user_id
        )
        if operator not in {"owner", "admin", "member"}:
            raise Rejected("无法确认操作者身份")
        privileged = self.protected(event.target, event.user_id)
        if not self_action and not privileged and operator not in {"owner", "admin"}:
            raise Rejected("操作者没有群管理权限")
        if user_id:
            target_role = await bot.capabilities.require(Capability.GROUP_ROLE).member_role(
                event.target.target_id, user_id
            )
            if target_role not in {"member", "admin", "owner"}:
                raise Rejected("无法确认目标成员身份，未执行操作")
            if target_role == "owner":
                raise Rejected("不能对群主执行该操作")
            if not self_action and self.protected(event.target, user_id):
                raise Rejected("该用户受保护，不能执行该操作")
            if target_role == "admin" and (
                own != "owner" or (operator != "owner" and not privileged and not self_action)
            ):
                raise Rejected("不能越级操作管理员")
        return own, operator

    async def execute(
        self,
        bot: BotClient,
        event: Event,
        action: str,
        *,
        user_id: str = "",
        duration: int = 600,
        title: str = "",
        message_id: str = "",
    ) -> ServiceResult:
        """所有入口共用权限、身份与连接校验，不在失败后换参数重试。"""
        self_action = action in {"self_mute", "apply_title"}
        if self_action:
            user_id = event.user_id
        try:
            self._online(bot, event.target)
            if not await self.authorizer.check(f"group_admin:{action}", event):
                raise Rejected("当前操作未获授权")
            if type(duration) is not int or not 0 <= duration <= 2592000:
                raise Rejected("禁言秒数必须在 0 到 2592000 之间")
            if not isinstance(title, str) or sum(1 if ord(char) <= 127 else 2 for char in title) > 12:
                raise Rejected("头衔不能超过 6 个中文字或 12 个英文字母")
            own, operator = await self._roles(bot, event, user_id, self_action)
            if action in {"set_admin", "unset_admin"}:
                if own != "owner" or (operator != "owner" and not self.protected(event.target, event.user_id)):
                    raise Rejected("仅群主及具有群主身份的 Bot 可调整管理员")
            if action in {"set_title", "remove_title", "apply_title"} and own != "owner":
                raise Rejected("设置头衔要求 Bot 为群主")
            if not await self.authorizer.check(f"group_admin:{action}", event):
                raise Rejected("操作权限已撤销")
            self._online(bot, event.target)
            group = event.target.target_id
            if action in {"mute_member", "self_mute", "unmute_member"}:
                await bot.capabilities.require(Capability.MEMBER_MODERATION).ban(
                    group, user_id, 0 if action == "unmute_member" else duration
                )
            elif action in {"kick_member", "ban_kick_member"}:
                await bot.capabilities.require(Capability.MEMBER_MODERATION).kick(
                    group, user_id, reject_add_request=action == "ban_kick_member"
                )
            elif action in {"mute_all_on", "mute_all_off"}:
                await bot.capabilities.require(Capability.MODERATION).set_whole_ban(
                    group, enabled=action == "mute_all_on"
                )
            elif action in {"set_admin", "unset_admin"}:
                await bot.capabilities.require(Capability.MODERATION).set_admin(
                    group, user_id, enabled=action == "set_admin"
                )
            elif action in {"set_title", "remove_title", "apply_title"}:
                await bot.capabilities.require(Capability.MODERATION).set_special_title(
                    group, user_id, "" if action == "remove_title" else title
                )
            elif action == "recall_msg":
                if not message_id:
                    raise Rejected("请引用需要撤回的消息")
                await bot.capabilities.require(Capability.RECALL).delete_message(message_id)
            elif action in {"set_essence", "unset_essence"}:
                if Capability.ESSENCE not in bot.capabilities:
                    raise UnsupportedCapability("当前实现不支持精华消息")
                if not message_id:
                    raise Rejected("请引用需要操作的消息")
                await bot.capabilities.require(Capability.ESSENCE).set_essence(
                    message_id, enabled=action == "set_essence"
                )
            else:
                raise Rejected("未知群管理操作")
        except Rejected as exc:
            return ServiceResult(False, str(exc))
        except UnsupportedCapability:
            return ServiceResult(False, "当前平台不支持此操作")
        except (ActionFailed, TimeoutError, OSError, ValueError):
            logger.opt(exception=True).warning("群管理身份校验或动作未完成: {}", action)
            return ServiceResult(False, "无法确认身份或平台拒绝操作，未重试")
        return ServiceResult(True, "操作成功")

    async def automatic_kick(self, bot: BotClient, target: SessionTarget, user_id: str) -> bool:
        """自动验证不虚构操作者，只处理已确认身份的普通成员。"""
        return await self.automatic(bot, target, user_id, "kick")

    async def automatic(
        self,
        bot: BotClient,
        target: SessionTarget,
        user_id: str,
        penalty: str,
        *,
        duration: int = 300,
        message_id: str = "",
    ) -> bool:
        self._online(bot, target)
        if type(duration) is not int or not 0 <= duration <= 2592000:
            raise ValueError("自动禁言时长无效")
        if not await self.authorizer.check_target("group_admin", target) or self.protected(target, user_id):
            return False
        own = await bot.capabilities.require(Capability.GROUP_ROLE).member_role(target.target_id, bot.bot_id)
        role = await bot.capabilities.require(Capability.GROUP_ROLE).member_role(target.target_id, user_id)
        if own not in {"owner", "admin"} or role != "member":
            return False
        if not await self.authorizer.check_target("group_admin", target) or self.protected(target, user_id):
            return False
        self._online(bot, target)
        if penalty == "kick":
            await bot.capabilities.require(Capability.MEMBER_MODERATION).kick(target.target_id, user_id)
        elif penalty == "mute":
            await bot.capabilities.require(Capability.MEMBER_MODERATION).ban(target.target_id, user_id, duration)
        elif penalty != "recall":
            raise ValueError("未知自动群管理处罚")
        if message_id:
            await bot.capabilities.require(Capability.RECALL).delete_message(message_id)
        return True
