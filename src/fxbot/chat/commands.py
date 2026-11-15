"""AI 对话命令：人格切换和重置会话。"""

from __future__ import annotations

from typing import Any

from loguru import logger

from ..core import Event, BotClient, SessionTarget, HandlerContext
from ..config import get_manager as get_config_manager
from .service import chat_service
from .bindings import PERSONA, PROVIDER, get_binding, set_binding
from .personas import DEFAULT_PERSONA, load_personas
from .providers import provider_manager
from ..core.matcher import Matcher, MatcherOptions, bind_matchers
from ..core.matching import MatchResult
from .message_adapter import session_id_for_event
from ..core.permission import Permission
from ..core.application import Application
from ..permission.types import PermLevel
from .behavior.reply_gate import (
    talk_value_for,
    proactive_switch,
    set_proactive_switch,
    default_reply_gate_registry,
)
from ..permission.authorization import Authorizer


def _chat_cfg() -> dict[str, Any]:
    """读取 AI 配置。"""
    return get_config_manager().get_system()["chat"]


_COMMAND_PREFIXES = ("/", "#")


def _strip_prefix(text: str) -> str | None:
    """去掉固定的命令前缀，没有前缀时返回 None。"""
    for prefix in _COMMAND_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :].lstrip()
    return None


def _persona_name(text: str) -> str | None:
    """解析人格命令，返回为空表示查询，返回 None 表示不是人格命令。"""
    stripped = _strip_prefix(text.strip())
    if stripped is None or not stripped.startswith("人格"):
        return None
    return stripped[len("人格") :].strip()


def _provider_name(text: str) -> str | None:
    """解析 Provider 命令，返回为空表示查询，返回 None 表示不是 Provider 命令。"""
    stripped = _strip_prefix(text.strip())
    if stripped is None:
        return None
    parts = stripped.split(maxsplit=1)
    if not parts or parts[0].lower() != "provider":
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def _proactive_argument(text: str) -> str | None:
    """解析「主动」命令，返回空表示查询，None 表示不是该命令。"""
    stripped = _strip_prefix(text.strip())
    if stripped is None:
        return None
    parts = stripped.split(maxsplit=1)
    if not parts or parts[0] != "主动":
        return None
    return parts[1].strip() if len(parts) > 1 else ""


async def _can_manage(event: Event, authorizer: Authorizer) -> bool:
    """私聊可管理自己的会话；群聊要求管理权限。"""
    return not event.is_group or authorizer.context(event, authorizer.storage.load()).user_level >= PermLevel.ADMIN


async def _handle_persona(bot: BotClient, event: Event, context: HandlerContext) -> None:
    """切换当前群或当前私聊用户的人格。"""
    group_id = event.target.target_id if event.is_group else None
    session_id = session_id_for_event(bot, event)
    scope = "本群" if group_id else ""

    name = _persona_name(event.message.plain_text()) or ""
    personas = load_personas()
    default_name = str(_chat_cfg()["persona"] or "") or DEFAULT_PERSONA

    if not name:
        bound = get_binding(PERSONA, session_id)
        effective = bound if bound in personas else default_name
        await context.finish(f"{scope}当前人格：{effective}\n可用人格：{'、'.join(sorted(personas))}")

    if name in {"重置", "恢复默认"}:
        try:
            set_binding(PERSONA, session_id, None)
        except OSError:
            logger.opt(exception=True).warning(f"[Chat] 清除人格绑定失败: session={session_id}")
            await context.finish("人格设置失败，请稍后再试")
        await chat_service.clear_session(session_id)
        await context.finish(f"已恢复全局默认人格：{default_name}，对话历史已清空")

    if name not in personas:
        await context.finish(f"没有名为「{name}」的人格，可用人格：{'、'.join(sorted(personas))}")

    try:
        set_binding(PERSONA, session_id, name)
    except OSError:
        logger.opt(exception=True).warning(f"[Chat] 保存人格绑定失败: session={session_id}")
        await context.finish("人格设置失败，请稍后再试")
    await chat_service.clear_session(session_id)
    await context.finish(f"{scope}人格已切换为「{name}」，对话历史已清空")


async def _handle_provider(bot: BotClient, event: Event, context: HandlerContext) -> None:
    """切换当前群或当前私聊用户使用的 Provider。"""
    group_id = event.target.target_id if event.is_group else None
    session_id = session_id_for_event(bot, event)
    scope = "本群" if group_id else "当前会话"

    name = _provider_name(event.message.plain_text()) or ""
    names = sorted(provider_manager.list_providers_by_type("chat"))
    default_name = str(_chat_cfg()["provider"] or "") or "自动（第一个对话 Provider）"
    choices = "、".join(names) or "（未配置）"

    if not name:
        bound = get_binding(PROVIDER, session_id)
        effective = bound if bound in names else default_name
        await context.finish(f"{scope}当前 Provider：{effective}\n可用 Provider：{choices}")

    if name in {"重置", "恢复默认"}:
        try:
            set_binding(PROVIDER, session_id, None)
        except OSError:
            logger.opt(exception=True).warning(f"[Chat] 清除 Provider 绑定失败: session={session_id}")
            await context.finish("Provider 设置失败，请稍后再试")
        await context.finish(f"已恢复全局默认 Provider：{default_name}")

    if name not in names:
        await context.finish(f"没有名为「{name}」的对话 Provider，可用 Provider：{choices}")

    try:
        set_binding(PROVIDER, session_id, name)
    except OSError:
        logger.opt(exception=True).warning(f"[Chat] 保存 Provider 绑定失败: session={session_id}")
        await context.finish("Provider 设置失败，请稍后再试")
    await context.finish(f"{scope} Provider 已切换为「{name}」")


async def _handle_clear(bot: BotClient, event: Event, context: HandlerContext) -> None:
    """销毁当前会话，连同长期记忆一起重新开始；学习记录保留。"""
    cleared = await chat_service.reset_session(session_id_for_event(bot, event))
    if cleared:
        await context.finish("已重置当前会话：历史与长期记忆已清空，学习记录保留")
    await context.finish("已重置当前会话；长期记忆清理失败，请查看日志")


def _switch_state(override: bool | None) -> str:
    return "未设置" if override is None else "开启" if override else "关闭"


async def _handle_proactive(bot: BotClient, event: Event, context: HandlerContext, authorizer: Authorizer) -> None:
    """查看或调整本群主动接话。"""
    group_id = (event.target.target_id if event.is_group else None) or ""
    tenant = session_id_for_event(bot, event)
    argument = _proactive_argument(event.message.plain_text()) or ""
    if argument in {"开", "开启", "打开", "关", "关闭"}:
        if not group_id:
            await context.finish("主动接话只用于群聊")
        if not await _can_manage(event, authorizer):
            await context.finish("只有群主或管理员可以调整主动接话")
        enabled = argument in {"开", "开启", "打开"}
        try:
            await set_proactive_switch(tenant, enabled)
        except OSError:
            logger.opt(exception=True).warning("[Chat] 写入主动接话开关失败")
            await context.finish("设置失败，请稍后再试")
        await context.finish("本群主动接话已关闭" if not enabled else "本群主动接话已开启")
    if argument:
        await context.finish("用法：#主动 / #主动 开 / #主动 关")

    override = await proactive_switch(tenant) if group_id else None
    snapshot = default_reply_gate_registry.peek(tenant)
    window = snapshot.snapshot(5)["window"] if snapshot is not None else {}
    lines = [
        f"本群主动接话：{'开启' if override is True else '关闭'}（{_switch_state(override)}）",
        f"talk_value：{talk_value_for(tenant):.2f}",
    ]
    if group_id:
        lines.append(
            "窗口：预计 {:.2f}，实际 {}（{} 轮）".format(
                float(window.get("expected_replies", 0.0)),
                int(window.get("actual_replies", 0)),
                int(window.get("scored_rounds", 0)),
            )
        )
    await context.finish("\n".join(lines))


def install(application: Application, authorizer: Authorizer) -> None:
    """注册显式原生命令，不创建框架权限或匹配器对象。"""

    async def management(event: Event) -> bool:
        return await authorizer.check("chat", event) and await _can_manage(event, authorizer)

    async def owner(event: Event) -> bool:
        key = SessionTarget(event.platform, event.bot_id, "private", event.user_id).key()
        return (
            await authorizer.check("chat", event)
            and key in authorizer.configuration.get_system()["system"]["superusers"]
        )

    async def persona(context: HandlerContext) -> None:
        await _handle_persona(context.bot, context.event, context)

    async def provider(context: HandlerContext) -> None:
        await _handle_provider(context.bot, context.event, context)

    async def clear(context: HandlerContext) -> None:
        await _handle_clear(context.bot, context.event, context)

    async def proactive(context: HandlerContext) -> None:
        await _handle_proactive(context.bot, context.event, context, authorizer)

    definitions = []
    for name, parser, handler, permission in (
        ("persona", _persona_name, persona, management),
        ("provider", _provider_name, provider, owner),
        ("clear", lambda text: "" if _strip_prefix(text.strip()) == "清空对话" else None, clear, management),
        ("proactive", _proactive_argument, proactive, authorizer.guard("chat")),
    ):

        def match(event, parse=parser):
            if not event.is_message:
                return None
            value = parse(event.message.plain_text())
            return MatchResult(value) if value is not None else None

        display_names = {
            "persona": "切换人格",
            "provider": "切换模型",
            "clear": "清空对话",
            "proactive": "主动回复设置",
        }
        matcher = Matcher(
            match,
            MatcherOptions(name=name, display_name=display_names[name], permission=Permission.for_event(permission)),
        )
        matcher.handle()(handler)
        definitions.append(matcher.snapshot())
    bind_matchers(application, "chat", tuple(definitions))
