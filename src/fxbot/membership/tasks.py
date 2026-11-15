"""应用拥有的会员到期处理与任务重排。"""

from __future__ import annotations

import asyncio
from datetime import timezone
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from fxbot.core.errors import ActionFailed

from ..core import Message, SessionTarget
from .guard import MembershipGuard
from .models import MembershipGroup, utc_now
from ..config import ConfigManager
from .contact import renewal_contact_text, format_membership_expiry
from .service import MembershipService
from ..core.scheduler import Job, Daily, Scheduler
from ..core.application import Application
from ..core.capabilities import Capability


@dataclass(frozen=True, slots=True)
class MembershipTaskResult:
    """本轮到期和成功退群的数量。"""

    left: int = 0
    expired: int = 0


class MembershipMaintenance:
    """通过显式应用连接执行会员维护，不搜索宿主框架。"""

    def __init__(
        self,
        application: Application,
        configuration: ConfigManager,
        service: MembershipService,
        guard: MembershipGuard,
        scheduler: Scheduler,
    ) -> None:
        """绑定唯一的应用、配置和调度器。"""
        self.application = application
        self.configuration = configuration
        self.service = service
        self.guard = guard
        self.scheduler = scheduler

    def configure(self) -> None:
        """根据当前有效配置显式新增或移除任务。"""
        config = self.configuration.get_system()["membership"]
        if not config["enable_scheduler"]:
            self.scheduler.remove("membership:expiry")
            return
        hour, minute = map(int, config["schedule_time"].split(":"))
        self.scheduler.set(Job("membership:expiry", Daily((hour,), minute), self.run))

    async def run(self) -> None:
        """执行前再次读取开关，已排入队列的旧任务不能绕过禁用。"""
        if not self.configuration.get_system()["membership"]["enable_scheduler"]:
            return
        result = await self.process()
        logger.info("[Membership] 到期 {} 个群，退出 {} 个群", result.expired, result.left)

    async def process(self) -> MembershipTaskResult:
        """逐群处理；单群故障只影响该群，不停止本轮其余工作。"""
        config = self.configuration.get_system()["membership"]
        groups = await self.service.list_groups()
        expired = left = 0
        for group in groups:
            try:
                result = await self._process_group(group, config["auto_leave_expired_groups"])
                expired += result.expired
                left += result.left
            except (SQLAlchemyError, ValueError, ConnectionError, ActionFailed, asyncio.TimeoutError):
                logger.opt(exception=True).error("会员群维护失败")
            if config["batch_delay_seconds"]:
                await asyncio.sleep(config["batch_delay_seconds"])
        return MembershipTaskResult(left=left, expired=expired)

    async def _process_group(self, group: MembershipGroup, auto_leave: bool) -> MembershipTaskResult:
        """先提交过期状态并刷新缓存，再执行外部发送和退群。"""
        if group.status != "active" or group.expires_at is None:
            return MembershipTaskResult()
        expires = group.expires_at if group.expires_at.tzinfo else group.expires_at.replace(tzinfo=timezone.utc)
        if expires > utc_now():
            return MembershipTaskResult()
        target = SessionTarget.from_key(group.group_id)
        if not await self.service.mark_expired(group.group_id):
            return MembershipTaskResult()
        await self.guard.invalidate(group.group_id)
        bot = next(
            (
                item
                for item in self.application.bots
                if (item.platform, item.bot_id) == (target.platform, target.bot_id)
            ),
            None,
        )
        if not auto_leave or bot is None:
            return MembershipTaskResult(expired=1)
        notice = f"本群会员已到期，到期时间：{format_membership_expiry(expires)}。\n{renewal_contact_text()}"
        try:
            await bot.send(target, Message.text(notice))
        except (ConnectionError, ActionFailed, asyncio.TimeoutError):
            logger.opt(exception=True).warning("到期提醒发送失败，继续执行退群")
        await bot.capabilities.require(Capability.GROUP_DIRECTORY).leave_group(target.target_id)
        return MembershipTaskResult(left=1, expired=1)
