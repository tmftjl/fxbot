"""群聊日报原生定时调度与精确目标交付。"""

from __future__ import annotations

from loguru import logger

from ...core import Message, BotClient, Capability, SessionTarget, MessageSegment, UnsupportedCapability
from .config import get_config, is_enabled, auto_groups, group_allowed, validate_config
from .report import generate, daily_period, group_name_for
from ...utils.tz import SHANGHAI_TZ
from ...core.errors import ActionFailed
from ...core.plugin import scheduled
from ...core.scheduler import Daily
from ...core.application import Application
from ...runtime.notifications import NotificationService

JOB_NAME = "group_analysis:daily"


class ReportSchedule:
    """配置保存后重新排期；按群号选择能查询到该群的在线 Bot。"""

    def __init__(self, application: Application, notifications: NotificationService) -> None:
        self.application = application
        self.notifications = notifications

    def _valid(self, target: SessionTarget) -> bool:
        return is_enabled() and target.target_id in auto_groups() and group_allowed(target.key())

    async def _resolve(self, group_id: str) -> tuple[BotClient, SessionTarget] | None:
        """配置只存群号，发送前找出能查询到该群的在线 Bot。"""
        for bot in self.application.bots:
            if Capability.HISTORY not in bot.capabilities or Capability.TARGET_LOOKUP not in bot.capabilities:
                continue
            target = SessionTarget(bot.platform, bot.bot_id, "group", group_id)
            try:
                await bot.capabilities.require(Capability.TARGET_LOOKUP).target_name(target)
            except (UnsupportedCapability, ActionFailed, TimeoutError, OSError, ValueError):
                continue
            return bot, target
        return None

    async def run(self) -> None:
        if not is_enabled():
            return
        end, label, report_date = daily_period()
        for group_id in tuple(auto_groups()):
            resolved = await self._resolve(group_id)
            if resolved is None:
                logger.info("自动群日报跳过: {} / 没有可用 Bot", group_id)
                continue
            bot, target = resolved
            key = target.key()
            if not await self.notifications.allowed("group_analysis", target, valid=lambda: self._valid(target)):
                continue
            if self.application.get_bot(target.platform, target.bot_id) is not bot:
                continue
            result = await generate(
                bot,
                target,
                days=1,
                end=end,
                label=label,
                report_date=report_date,
                group_name=await group_name_for(bot, target),
                session_id=key,
            )
            if not result.ok:
                logger.info("自动群日报跳过: {} / {}", key, result.reason)
                continue
            await self.notifications.send(
                "group_analysis",
                target,
                Message([MessageSegment.image(result.image)]),
                valid=lambda: self._valid(target) and self.application.is_registered(bot),
            )


def daily_plan(context):
    config = get_config()
    validate_config(config)
    if not config["basic"]["enabled"] or not config["auto"]["auto_groups"]:
        return None
    auto = config["auto"]
    return Daily((auto["auto_hour"],), auto["auto_minute"], SHANGHAI_TZ)


@scheduled(plan=daily_plan, watch_config=True, name="daily")
async def daily(context):
    await ReportSchedule(context.app_services.application, context.notifications).run()
