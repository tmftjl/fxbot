"""按完整会话身份判定会员资格并缓存业务结果。"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from ..core import SessionTarget
from .models import MembershipGroup, utc_now
from ..config import ConfigManager, get_manager
from .service import MembershipService, membership_service
from ..utils.cache import cache


@dataclass(frozen=True, slots=True)
class MembershipDecision:
    """不包含内部异常内容的会员准入结果。"""

    allowed: bool
    reason: str
    expires_at: datetime | None = None
    status: str = ""


class MembershipGuard:
    """依赖配置和会员服务，豁免判断先于业务缓存。"""

    def __init__(self, configuration: ConfigManager, service: MembershipService) -> None:
        """显式绑定业务依赖。"""
        self.configuration = configuration
        self.service = service

    async def check_membership_detail(self, group_id: str, user_id: str | None) -> MembershipDecision:
        """群和操作者都必须是规范键，后台任务使用明确的空操作者。"""
        group = SessionTarget.from_key(group_id)
        if group.kind != "group":
            raise ValueError("会员查询目标必须为群")
        if user_id is not None:
            user = SessionTarget.from_key(user_id)
            if user.kind != "private" or (user.platform, user.bot_id) != (group.platform, group.bot_id):
                raise ValueError("会员操作者不属于目标连接")
            system = self.configuration.get_system()["system"]
            if user_id in system["superusers"]:
                return MembershipDecision(True, "exempt_superuser")
            if user_id in system["bot_admins"]:
                return MembershipDecision(True, "exempt_bot_admin")
        key = ("membership", group_id)
        decision = cache.get(key)
        if decision is None:
            decision = await self._load_from_db(group_id)
            cache.set(key, decision)
        if decision.expires_at is not None and decision.expires_at <= utc_now():
            return MembershipDecision(False, "expired", decision.expires_at, decision.status)
        return decision

    async def invalidate(self, group_id: str) -> None:
        """写入后立即刷新对应群，不影响无关会话。"""
        key = ("membership", group_id)
        cache.pop(key)
        cache.set(key, await self._load_from_db(group_id))

    async def _load_from_db(self, group_id: str) -> MembershipDecision:
        """数据库查询失败在这一业务边界闭锁，不返回异常详情。"""
        try:
            record = await self.service.get_group(group_id)
        except SQLAlchemyError:
            logger.opt(exception=True).warning("会员数据库查询失败，拒绝业务访问")
            return MembershipDecision(False, "db_error")
        if record is None:
            return MembershipDecision(False, "not_registered")
        return self._decision(record)

    @staticmethod
    def _decision(record: MembershipGroup) -> MembershipDecision:
        """SQLite 无时区时间一律解释为 UTC。"""
        expires = record.expires_at
        if expires is not None:
            expires = (
                expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires.astimezone(timezone.utc)
            )
        if record.status != "active":
            return MembershipDecision(False, f"status_{record.status}", expires, record.status)
        if expires is not None and expires <= utc_now():
            return MembershipDecision(False, "expired", expires, record.status)
        return MembershipDecision(True, "active" if expires else "active_no_expiry", expires, record.status)


membership_guard = MembershipGuard(get_manager(), membership_service)
