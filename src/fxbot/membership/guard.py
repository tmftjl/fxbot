"""按平台裸群号判定会员资格并缓存业务结果。"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from .models import MembershipGroup, utc_now
from ..config import ConfigManager, get_manager
from .service import MembershipService, membership_service
from ..utils.cache import cache
from ..permission.identity import is_superuser_identity


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
        """群和操作者都使用平台裸 ID，后台任务使用明确的空操作者。"""
        group = str(group_id or "").strip()
        if not group:
            raise ValueError("会员查询目标不能为空")
        user = str(user_id or "").strip()
        if user:
            system = self.configuration.get_system()["system"]
            if is_superuser_identity(system, user):
                return MembershipDecision(True, "exempt_superuser")
            if user in system["bot_admins"]:
                return MembershipDecision(True, "exempt_bot_admin")
        key = ("membership", group)
        decision = cache.get(key)
        if decision is None:
            decision = await self._load_from_db(group)
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
