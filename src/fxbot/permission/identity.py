"""超级用户身份匹配；完整规范键和裸平台用户 ID 都接受。"""

from __future__ import annotations

from ..core import SessionTarget


def superuser_entries(system: dict | None) -> set[str]:
    """读取配置中的超级用户条目，忽略空值。"""
    values = (system or {}).get("superusers") or []
    return {str(item).strip() for item in values if str(item).strip()}


def is_superuser_identity(system: dict | None, identity: str) -> bool:
    """判断规范私聊键或其裸用户 ID 是否在超级用户名单中。"""
    entries = superuser_entries(system)
    if not entries:
        return False
    value = str(identity or "").strip()
    if not value:
        return False
    if value in entries:
        return True
    try:
        target = SessionTarget.from_key(value)
    except ValueError:
        return False
    return target.kind == "private" and target.target_id in entries


def is_superuser_event(system: dict | None, event) -> bool:
    """按事件实际平台身份判断，不接受群聊键冒充私聊身份。"""
    identity = SessionTarget(event.platform, event.bot_id, "private", event.user_id).key()
    return is_superuser_identity(system, identity)
