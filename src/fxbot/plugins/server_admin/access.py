"""服务器运维独立的失败封闭权限检查。"""

from __future__ import annotations

from loguru import logger

from .store import enabled, base_session, server_session, is_server_session
from ...core import Event, BotClient, SessionTarget
from ...config import get_manager
from ...permission import get_storage
from ...permission.identity import is_superuser_identity

GROUP_EVENT_ATTR = "_fxbot_server_group"


def normalize_id(value) -> str | None:
    """标准化平台 ID。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text != "0" else None


def event_plain_text(event: Event) -> str:
    """提取事件纯文本；消息结构异常时按空文本处理。"""
    try:
        return str(event.message.plain_text() or "").strip()
    except (AttributeError, ValueError):
        return ""


def _system() -> dict:
    """读取核心系统配置；读取失败时按没有超级用户处理。"""
    try:
        return get_manager().get_system().get("system", {})
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] 超级用户配置不可用")
        return {}


def is_superuser(identity: str) -> bool:
    """接受核心 system.superusers 的完整键或裸用户 ID；机器人管理员不授予此身份。"""
    try:
        return is_superuser_identity(_system(), str(identity or ""))
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] 超级用户身份校验失败")
        return False


def plugin_enabled(command: str = "mode") -> bool:
    """显式要求权限节点存在且开关开启，缺配置不放行。"""
    try:
        config = get_storage().load()
        node = config["sub_plugins"]["server_admin"]
        return (
            config["top"].get("enabled") is True
            and node["top"].get("enabled") is True
            and (node["commands"].get(command) or {}).get("enabled") is True
        )
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] 插件开关不可用")
        return False


def _base_for(bot: BotClient, user_id) -> str:
    """用真实平台、Bot 和用户号构造规范私聊基础会话。"""
    user = str(user_id or "").strip()
    platform = str(getattr(bot, "platform", "") or "")
    bot_id = str(getattr(bot, "bot_id", "") or "")
    if not user or not platform or not bot_id:
        raise ValueError("身份缺失")
    return SessionTarget(platform, bot_id, "private", user).key()


def actor_base(bot: BotClient, event: Event) -> str:
    """从真实平台事件取身份，不经过会话解析器，防止伪造工具上下文。"""
    if bot is None or event is None or event.target.kind != "private":
        raise ValueError("仅限超级用户私聊")
    return _base_for(bot, event.user_id)


def group_actor_base(bot: BotClient, event: Event) -> str:
    """群聊触发时用发送者的私聊身份派生同一服务器会话。"""
    if bot is None or event is None or event.target.kind != "group" or not event.target.target_id:
        raise ValueError("仅限群聊")
    return _base_for(bot, event.user_id)


def mark_group_event(event: Event) -> None:
    """标记这条群消息已经过 #服务器模式# 入口，供工具执行时复核。"""
    event.metadata[GROUP_EVENT_ATTR] = True


def _marked_group(event: Event) -> bool:
    """只认本进程在真实事件元数据上写入的入口标记。"""
    return isinstance(event.metadata, dict) and event.metadata.get(GROUP_EVENT_ATTR) is True


def owner_allowed(bot: BotClient, event: Event) -> bool:
    """真实私聊加显式超级用户和插件开关，任何异常均拒绝。"""
    try:
        identity = actor_base(bot, event)
        return plugin_enabled() and is_superuser(identity)
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] 超级用户校验未通过")
        return False


def owner_allowed_group(bot: BotClient, event: Event) -> bool:
    """群聊入口同样要求真实群事件、超级用户和插件开关。"""
    try:
        identity = group_actor_base(bot, event)
        return plugin_enabled("group_mode") and is_superuser(identity)
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] 群聊超级用户校验未通过")
        return False


def execution_allowed(session_id: str, bot: BotClient, event: Event, user_id: str = "", group_id=None) -> bool:
    """校验派生会话、实际事件、模式状态及工具身份均一致。"""
    try:
        if not event or not is_server_session(session_id):
            return False
        if user_id and user_id != str(event.user_id):
            return False
        if _marked_group(event):
            # 群聊只认 #服务器模式# 入口标记过、且属于同一超级用户私聊域的会话。
            base = group_actor_base(bot, event)
            return (
                not group_id
                and event.target.kind == "group"
                and plugin_enabled("group_mode")
                and is_superuser(base)
                and server_session(base) == session_id
                and enabled(session_id)
            )
        base = actor_base(bot, event)
        return (
            not group_id
            and owner_allowed(bot, event)
            and base_session(session_id) == base
            and server_session(base) == session_id
            and enabled(session_id)
        )
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] 执行边界校验未通过")
        return False
