"""后台通知的精确连接选择、动态准入与实际交付。"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from fxbot.core.errors import ActionFailed

from ..core import Message, SessionTarget, UnsupportedCapability
from ..config import ConfigManager
from ..core.application import Application
from ..membership.guard import MembershipGuard
from ..permission.authorization import Authorizer


class NotificationService:
    """发送前重查策略和订阅，不能借用其他平台或账号的连接。"""

    def __init__(
        self,
        application: Application,
        configuration: ConfigManager,
        authorizer: Authorizer,
        membership: MembershipGuard,
    ) -> None:
        self.application = application
        self.configuration = configuration
        self.authorizer = authorizer
        self.membership = membership

    async def allowed(self, feature: str, target: SessionTarget, *, valid: Callable[[], bool]) -> bool:
        """昂贵的后台生成开始前也必须通过同一准入策略。"""
        bot = self.application.get_bot(target.platform, target.bot_id)
        if bot is None or not valid() or not await self.authorizer.check_target(feature, target):
            return False
        settings = self.configuration.get_system()["membership"]
        if target.kind == "group" and settings["enabled"] and target.bot_key() not in settings["free_bot_ids"]:
            decision = await self.membership.check_membership_detail(target.target_id, None)
            if not decision.allowed:
                return False
        if not valid() or self.application.get_bot(target.platform, target.bot_id) is not bot:
            return False
        return True

    async def send(self, feature: str, target: SessionTarget, message: Message, *, valid: Callable[[], bool]) -> bool:
        """False 表示本次未交付；未知实现错误向任务所有者报告。"""
        bot = self.application.get_bot(target.platform, target.bot_id)
        if bot is None or not await self.allowed(feature, target, valid=valid):
            return False
        if not valid() or self.application.get_bot(target.platform, target.bot_id) is not bot:
            return False
        try:
            await bot.send(target, message)
        except (ActionFailed, UnsupportedCapability, TimeoutError, OSError):
            logger.opt(exception=True).warning("后台通知未交付: {} / {}", feature, target.key())
            return False
        return True
