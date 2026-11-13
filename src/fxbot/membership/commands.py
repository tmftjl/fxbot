"""直接注册到核心路由的会员自助和管理命令。"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..core import Event, SessionTarget, HandlerContext
from .guard import MembershipGuard
from .tasks import MembershipMaintenance
from ..config import ConfigManager
from .contact import renewal_contact_text, format_membership_expiry
from .service import MembershipError, MembershipService
from ..console.auth import rotate_console_token
from ..core.application import Application
from ..runtime.settings import Settings


class MembershipCommands:
    """显式持有业务依赖，处理器只接收核心上下文。"""

    def __init__(
        self,
        application: Application,
        config: ConfigManager,
        service: MembershipService,
        guard: MembershipGuard,
        settings: Settings,
        maintenance: MembershipMaintenance,
    ) -> None:
        """绑定本应用的依赖，不查询全局框架注册表。"""
        self.application = application
        self.config = config
        self.service = service
        self.guard = guard
        self.settings = settings
        self.maintenance = maintenance

    async def owner(self, event: Event) -> bool:
        """管理命令同时要求私聊和明确的完整主人身份。"""
        identity = SessionTarget(event.platform, event.bot_id, "private", event.user_id).key()
        return event.target.kind == "private" and identity in self.config.get_system()["system"]["superusers"]

    def install(self) -> None:
        """注册自助命令和独立保护的管理命令。"""
        router = self.application.router
        router.regex("membership:renew", r"^ww续费[A-Za-z0-9_]+$", self.renew)
        router.regex("membership:expiry", r"^ww到期$", self.expiry)
        router.regex("membership:prompt", r"^ww(?:拉群|续费)$", self.prompt)
        router.regex("membership:generate", r"^ww生成续费码\d+(?:天|月|年)$", self.generate, permission=self.owner)
        router.regex("membership:console_login", r"^今汐登录$", self.console_login, permission=self.owner)
        router.regex("membership:adjust", r"^ww续费调整$", self.adjust_bots, permission=self.owner)
        router.regex("membership:check", r"^ww检查会员$", self.check, permission=self.owner)

    async def renew(self, context: HandlerContext) -> None:
        """在当前完整群身份下兑换，成功后立即刷新门禁缓存。"""
        if not context.event.is_group:
            await context.finish("续费码只能在群聊中使用哦")
        group = context.event.target.key()
        code = context.arguments.removeprefix("ww续费")
        row = await self.service.get_code(code)
        if row is None or row.status != "active" or row.used_count >= row.max_use:
            await context.finish("该续费码无效或已被使用")
        try:
            result = await self.service.redeem_code(
                code,
                group,
                operator_user_id=context.event.user_id,
                managed_by_bot=context.event.target.bot_key(),
            )
        except MembershipError:
            await context.finish("该续费码当前不可用，请联系管理员核对")
        await self.guard.invalidate(group)
        units = {"day": "天", "month": "月", "year": "年"}
        duration = f"{row.duration_value}{units[row.duration_unit]}"
        date = format_membership_expiry(result.after_expires_at)
        await context.finish(f"本群会员已成功续费{duration}，到期时间：{date}")

    async def expiry(self, context: HandlerContext) -> None:
        """直接查询群会员，不依赖已经放行的门禁缓存。"""
        if not context.event.is_group:
            await context.finish("该指令需在群聊中使用")
        group = await self.service.get_group(context.event.target.key())
        if group is None:
            await context.finish("未找到本群的会员记录")
        expires = group.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        days = (expires.date() - datetime.now(timezone.utc).date()).days if expires else None
        if group.status != "active" or days is not None and days < 0:
            status = "已到期"
        elif days == 0:
            status = "今天到期"
        else:
            status = "有效" if days is None else f"有效(剩余{days}天)"
        date = "永久" if expires is None else format_membership_expiry(expires)
        reply = f"本群会员状态：{status}\n到期时间：{date}"
        if expires is not None:
            reply += f"\n{renewal_contact_text()}"
        await context.finish(reply)

    async def prompt(self, context: HandlerContext) -> None:
        """提供首次开通和续费入口。"""
        await context.finish("如需首次开通或续费，请联系管理员购买续费码，在群内直接发送即可生效")

    async def generate(self, context: HandlerContext) -> None:
        """仅主人私聊生成一次性续费码。"""
        match = re.fullmatch(r"ww生成续费码(\d+)(天|月|年)", context.arguments)
        duration = int(match[1])
        if duration <= 0:
            await context.finish("续费时长必须大于零")
        units = {"天": "day", "月": "month", "年": "year"}
        row = await self.service.generate_code(duration_value=duration, duration_unit=units[match[2]], max_use=1)
        await context.finish(f"已生成一次性续费码：ww续费{row.code}\n请在需要开通或续费的群中发送")

    async def console_login(self, context: HandlerContext) -> None:
        """仅向主人私聊发送重置后的控制台访问令牌。"""
        token = rotate_console_token()
        await context.finish(f"控制台登录地址：http://{self.settings.host}:{self.settings.port}/fxbot?token={token}")

    async def check(self, context: HandlerContext) -> None:
        """主人显式触发一次会员维护。"""
        result = await self.maintenance.process()
        await context.finish(f"已处理过期{result.expired}个群，退出{result.left}个群")

    async def adjust_bots(self, context: HandlerContext) -> None:
        """根据完整平台会话身份校准已登记群的管理连接。"""
        records = {row.group_id: row for row in await self.service.list_groups()}
        changed = unchanged = 0
        for bot in self.application.bots:
            groups = await bot.get_groups()
            for group in groups:
                target = SessionTarget(bot.platform, bot.bot_id, "group", str(group["group_id"]))
                row = records.get(target.key())
                if row is None:
                    continue
                manager = target.bot_key()
                if row.managed_by_bot == manager:
                    unchanged += 1
                else:
                    await self.service.update_managed_bot(row.group_id, manager)
                    changed += 1
                    await self.guard.invalidate(row.group_id)
        await context.finish(f"续费调整完成\n更新: {changed} 个群\n未变: {unchanged} 个群")
