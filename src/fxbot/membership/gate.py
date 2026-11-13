"""基于核心事件的群会员准入和到期提醒。"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..core import Event, Message, SessionTarget, current_runtime
from .guard import MembershipGuard
from ..config import ConfigManager
from .contact import renewal_contact_text, format_membership_expiry

RECOVERY_COMMAND = re.compile(r"^(?:ww到期|ww(?:拉群|续费)|ww续费[A-Za-z0-9_]+)$")


class MembershipGate:
    """会员判定属于准入阶段，恢复命令先于数据库查询放行。"""

    def __init__(self, configuration: ConfigManager, guard: MembershipGuard) -> None:
        """注入配置和会员判定，不注册框架钩子。"""
        self.configuration = configuration
        self.guard = guard

    async def __call__(self, event: Event) -> bool:
        """使用完整会话身份查询，数据库故障由判定层拒绝。"""
        if not event.is_message or not event.is_group:
            return True
        config = self.configuration.get_system()["membership"]
        if not config["enabled"]:
            return True
        if event.target.bot_key() in config["free_bot_ids"]:
            return True
        text = event.message.plain_text().strip()
        if RECOVERY_COMMAND.fullmatch(text):
            return True
        user = SessionTarget(event.platform, event.bot_id, "private", event.user_id).key()
        decision = await self.guard.check_membership_detail(event.target.key(), user)
        if not decision.allowed:
            return False
        if decision.expires_at is not None and any(
            text.startswith(prefix) for prefix in config["expire_prompt_text_prefixes"]
        ):
            days = (decision.expires_at.date() - datetime.now(timezone.utc).date()).days
            if 0 <= days <= config["expire_notice_days"]:
                date = format_membership_expiry(decision.expires_at)
                reminder = (
                    f"本群会员今天到期，到期时间：{date}。"
                    if days == 0
                    else f"本群会员将在 {days} 天后到期，到期时间：{date}。"
                )
                await current_runtime().bot.send(event.target, Message.text(f"{reminder}\n{renewal_contact_text()}"))
        return True
