"""显式注册的 AI 兜底、群聊上下文与 Focus 分发。"""

from __future__ import annotations

import re
import asyncio
from typing import Any
from contextvars import ContextVar

from loguru import logger

from . import emoji as emoji_module
from ..core import Event, BotClient, ReplySender, HandlerContext
from .focus import focus_mode_manager
from .hooks import ChatHookContext, guard, notify, has_chat_hooks
from .tools import ToolRuntime
from .types import ChatRequest, TriggerKind
from .voice import audio_segments, transcribe_event
from .access import id_allowlist as _id_allowlist
from ..config import get_manager as get_config_manager
from .service import chat_service, history_limit
from .personas import effective_persona_name
from ..core.matcher import bind_matchers
from .session_hooks import direct_reply_mode
from .work_schedule import can_reply
from ..core.commands import on_message
from .behavior.quote import quote_counter
from .message_adapter import (
    message_text,
    mention_flags,
    event_identity,
    collect_mentions,
    platform_for_bot,
    adapt_message_event,
    message_occurred_at,
    read_quote_snapshot,
    memory_image_sources,
    session_id_for_event,
    image_sources_for_event,
)
from ..core.application import Application
from .behavior.debounce import default_debounce_registry as default_debounce_registry
from .focus.group_runtime import FocusMember, FocusSender
from .behavior.turn_scheduler import normal_scheduler
from ..permission.authorization import Authorizer


def _persona_trigger(cfg: dict[str, Any], bot: BotClient, event: Event) -> str:
    """消息以当前人格名或别名开头时返回命中的名字，用于免 @ 叫醒 BotClient。"""
    if not event.is_group:
        return ""
    text = event.message.plain_text().lstrip()
    if not text:
        return ""
    behavior = cfg.get("behavior")
    aliases = behavior.get("persona_aliases") if isinstance(behavior, dict) else None
    names = [effective_persona_name(session_id_for_event(bot, event), cfg.get("persona"))]
    names.extend(str(alias).strip() for alias in aliases or [] if str(alias).strip())
    for name in names:
        if name and text.casefold().startswith(name.casefold()):
            return name
    return ""


def _chat_cfg() -> dict[str, Any]:
    """读取 AI 配置。"""
    cfg = get_config_manager().get_system()
    return cfg["chat"]


def _has_image(event: Event) -> bool:
    """检测规范化后的图片段。"""
    return any(segment.type == "image" for segment in (event.original_message or event.message).segments)


def _has_voice(event: Event) -> bool:
    """判断消息是否携带语音，用于放行纯语音消息；转写在适配阶段完成。"""
    return bool(audio_segments(event))


def _allowed_group_id(cfg: dict[str, Any], event: Event) -> str | None:
    """返回允许使用 AI 的群 ID，不在白名单内时返回 None。"""
    group_id = event.target.target_id if event.is_group else None
    if not group_id:
        return None
    allowlist = _id_allowlist(cfg, "group_allowlist")
    return group_id if not allowlist or event.target.key() in allowlist else None


def _message_filtered(cfg: dict[str, Any], event: Event) -> bool:
    """命中屏蔽词或正则的消息既不进入 AI，也不写历史。"""
    text = event.message.plain_text()
    if not text:
        return False
    settings = cfg["message_receive"]
    return any(word and word in text for word in settings["ban_words"]) or any(
        pattern and re.search(pattern, text) is not None for pattern in settings["ban_msgs_regex"]
    )


def _reply_timing(cfg: dict[str, Any]) -> dict[str, Any]:
    """回复时机配置；缺失时按默认值。"""
    return cfg["reply_timing"]


def _match_context(cfg: dict[str, Any], bot: BotClient, event: Event) -> bool:
    """按会话类型判断是否允许进入 AI 兜底；关闭的必回复项交给上下文记录。"""
    if event.is_group:
        if not _allowed_group_id(cfg, event):
            return False
        timing = _reply_timing(cfg)
        if event.to_me:
            return bool(timing["inevitable_at_reply"])
        if _persona_trigger(cfg, bot, event):
            return bool(timing["mentioned_bot_reply"])
        return False
    if not bool(cfg["private_enabled"]):
        return False
    allowlist = _id_allowlist(cfg, "private_allowlist")
    if allowlist and event.target.key() not in allowlist:
        return False
    return True


def _should_record_context(cfg: dict[str, Any], bot: BotClient, event: Event) -> bool:
    """不能进入 AI 回复的普通群消息写历史，包括关闭必回复的提及。"""
    if event.is_group:
        if not _allowed_group_id(cfg, event):
            return False
        timing = _reply_timing(cfg)
        if event.to_me and timing["inevitable_at_reply"]:
            return False
        if _persona_trigger(cfg, bot, event) and timing["mentioned_bot_reply"]:
            return False
        return True
    if not bool(cfg["private_enabled"]):
        return False
    allowlist = _id_allowlist(cfg, "private_allowlist")
    if allowlist and event.target.key() not in allowlist:
        return False
    return False


async def _ai_fallback_rule(bot: BotClient, event: Event) -> bool:
    """AI 兜底匹配规则。"""
    cfg = _chat_cfg()
    session_id = session_id_for_event(bot, event)
    if not can_reply(cfg, session_id=session_id) or _message_filtered(cfg, event):
        return False
    if not _match_context(cfg, bot, event):
        return False
    text = message_text(event)
    if not text and not _has_image(event) and not _has_voice(event):
        return False
    return await _ingress_allowed(bot, event, path="reply")


async def _context_rule(bot: BotClient, event: Event) -> bool:
    """记录无需回复的群聊或私聊消息，作为后续对话的上下文。"""
    cfg = _chat_cfg()
    if not bool(cfg["enabled"]) or history_limit(cfg, event.is_group) <= 0:
        return False
    if _message_filtered(cfg, event):
        return False
    if not _should_record_context(cfg, bot, event):
        return False
    text = message_text(event)
    has_image = _has_image(event)
    has_voice = _has_voice(event)
    if not text and not has_image and not has_voice:
        return False
    return await _ingress_allowed(bot, event, path="context")


async def _ingress_allowed(bot: BotClient, event: Event, *, path: str) -> bool:
    """同一事件与会话只判定一次，首个路由路径确定通知语义，后续询问复用结果。"""
    decisions = event.metadata.get(_INGRESS_ATTR)
    hooks_enabled = has_chat_hooks("ingress_guard", "observer")
    if not hooks_enabled and not isinstance(decisions, dict):
        return True
    session_id = session_id_for_event(bot, event)
    platform = platform_for_bot(bot)
    key = (platform, bot.bot_id, session_id)
    if not isinstance(decisions, dict):
        decisions = {}
        event.metadata[_INGRESS_ATTR] = decisions
    existing = decisions.get(key)
    pending_key = (id(event), key)
    if isinstance(existing, asyncio.Future):
        if not existing.done() and pending_key in _INGRESS_PENDING.get():
            raise RuntimeError("入站守卫不能递归判定同一事件")
        # 取消重复询问者不能取消首个路由仍在运行的守卫。
        return await asyncio.shield(existing)
    if not hooks_enabled:
        return True
    decision = asyncio.get_running_loop().create_future()
    decisions[key] = decision
    token = _INGRESS_PENDING.set(_INGRESS_PENDING.get() | {pending_key})
    try:
        context = ChatHookContext(
            session_id,
            event.user_id or "",
            (event.target.target_id if event.is_group else None),
            platform=platform,
            phase="ingress",
        )
        allowed = await guard("ingress_guard", context, path=path, text=message_text(event))
        await notify("ingress_accepted" if allowed else "ingress_rejected", context, path=path)
    except BaseException:
        decision.cancel()
        raise
    finally:
        _INGRESS_PENDING.reset(token)
    decision.set_result(allowed)
    return allowed


_INGRESS_ATTR = "_fxbot_chat_ingress_decisions"
_INGRESS_PENDING: ContextVar[frozenset[tuple[int, tuple[str, str, str]]]] = ContextVar(
    "chat_ingress_pending", default=frozenset()
)


async def _handle_context(bot: BotClient, event: Event, application: Application, authorizer: Authorizer) -> None:
    """把群聊或私聊的上下文消息写入 AI 会话历史。"""
    identity = event_identity(bot, event)
    if direct_reply_mode(identity.session_id):
        return
    if not chat_service.session_store.mark_seen(identity.session_id, identity.dedupe_key):
        return
    quote_counter(identity.session_id).record(identity.message_id or "", event.user_id or "")
    emoji_module.collect_in_background(identity.session_id, bot, event, application)
    has_image = _has_image(event)
    text = message_text(event)
    user_id = event.user_id or ""
    if not text and not has_image and _has_voice(event):
        # 纯语音先转写再入历史，转写失败时留占位
        text = await transcribe_event(bot, event)
    images = await image_sources_for_event(bot, event) if has_image else []
    # 上下文消息只做廉价提取：昵称取消息段里现成的，不额外查成员接口
    mentions = await collect_mentions(
        bot, event, (event.target.target_id if event.is_group else None), resolve_names=False
    )
    await chat_service.record_group_message(
        identity.session_id,
        user_id,
        event.user_name,
        text,
        is_group=bool((event.target.target_id if event.is_group else None)),
        images=images,
        has_image=has_image,
        mentions=mentions,
        dedupe_key=identity.dedupe_key,
        platform=identity.platform,
        event_id=identity.event_id,
        message_id=identity.message_id,
        reply_to_message_id=identity.reply_to_message_id,
        memory_images=await memory_image_sources(bot, event),
        has_at_bot=mention_flags(bot, event)[0],
        occurred_at=message_occurred_at(event),
        sender_role=event.metadata.get("sender_role"),
        quote_snapshot=await read_quote_snapshot(bot, event),
    )
    if not can_reply(_chat_cfg(), session_id=identity.session_id):
        return
    if focus_mode_manager.enabled() or focus_mode_manager.attached(identity.session_id):
        focus_request = await adapt_message_event(bot, event, trigger_kind="mention")
        # 这条消息刚由 record_group_message 写入历史，Focus 回复时不能再写一遍
        focus_request.metadata["history_recorded"] = True
        await _focus_or_detach(bot, event, focus_request, application, authorizer, is_at=not focus_request.is_group)
        if focus_mode_manager.is_enabled_for_request(focus_request):
            return
    if event.is_group:
        request = await adapt_message_event(bot, event, trigger_kind="proactive")
        request.accepted_epoch = chat_service.session_store.epoch(identity.session_id)
        request.activity_seq = chat_service.session_store.activity(identity.session_id)
        request.metadata.update({"history_recorded": True, "history_appended": True})
        has_at_bot, at_other = mention_flags(bot, event)
        request.metadata.update({"has_at_bot": has_at_bot, "at_other": at_other})
        sender = FocusSender(bot, event.target)
        runtime = ToolRuntime(bot=bot, event=event, sender=sender, application=application, authorizer=authorizer)
        await normal_scheduler.submit(chat_service, request, runtime)


async def _register_focus(
    bot: BotClient, event: Event, request: ChatRequest, application: Application, authorizer: Authorizer, *, is_at: bool
) -> None:
    """把入站消息交给共享组；是否回复由共享规划器统一决定。"""
    member = FocusMember(
        session_id=request.session_id,
        bot=bot,
        event=event,
        target=event.target,
        request=request,
        is_group=request.is_group,
        application=application,
        authorizer=authorizer,
    )
    await focus_mode_manager.register(member, request, is_at=is_at)


async def _focus_or_detach(
    bot: BotClient, event: Event, request: ChatRequest, application: Application, authorizer: Authorizer, *, is_at: bool
) -> bool:
    """Focus 接管时登记并返回 True；已不满足配置时让该聊天退回普通对话。"""
    if focus_mode_manager.is_enabled_for_request(request):
        await _register_focus(bot, event, request, application, authorizer, is_at=is_at)
        return True
    if focus_mode_manager.attached(request.session_id):
        await focus_mode_manager.detach(request.session_id)
    return False


async def _handle_ai_fallback(
    bot: BotClient, event: Event, sender: ReplySender, application: Application, authorizer: Authorizer
) -> None:
    """处理 AI 兜底消息；首个触发消息等完防抖窗口后统一生成一轮。"""
    identity = event_identity(bot, event)
    direct = direct_reply_mode(identity.session_id)
    if not chat_service.session_store.mark_seen(identity.session_id, identity.dedupe_key):
        logger.debug("[Chat] 忽略重复投递的 AI 消息")
        return
    quote_counter(identity.session_id).record(identity.message_id or "", event.user_id or "")
    if not direct:
        emoji_module.collect_in_background(identity.session_id, bot, event, application)
    trigger_kind: TriggerKind = "private" if not event.is_group else "mention" if event.to_me else "persona"
    # 序号按到达顺序分配：适配会下载图片、转写语音，放到适配之后会让慢消息拿到更新的序号
    await chat_service.ensure_hydrated(
        identity.session_id, is_group=bool((event.target.target_id if event.is_group else None))
    )
    seq_owner = "\0proactive" if trigger_kind == "proactive" else event.user_id or ""
    trigger_seq = chat_service.session_store.next_seq(identity.session_id, seq_owner)
    accepted_epoch = chat_service.session_store.epoch(identity.session_id)
    activity_seq = chat_service.session_store.activity(identity.session_id)
    if direct:
        request = await adapt_message_event(bot, event, trigger_kind=trigger_kind, include_quote=False)
    else:
        request = await adapt_message_event(bot, event, trigger_kind=trigger_kind)
    if await _focus_or_detach(
        bot, event, request, application, authorizer, is_at=trigger_kind in {"mention", "persona", "private"}
    ):
        # 共享组自己负责防抖、规划与发送，这里不再走普通轮次
        return
    if not request.text.strip() and not request.images:
        logger.debug("[Chat] 这条消息没有可回复的内容，跳过本轮")
        return
    # 序号与代次在进入 turn_lock 前捕获，用于丢弃被新消息或清空取代的旧回复
    request.trigger_seq = trigger_seq
    request.trigger_owner = seq_owner
    request.accepted_epoch = accepted_epoch
    request.activity_seq = activity_seq
    runtime = ToolRuntime(bot=bot, event=event, sender=sender, application=application, authorizer=authorizer)
    if direct:
        await chat_service.process_and_send(request, runtime)
        return
    await chat_service.record_group_message(
        request.session_id,
        request.user_id,
        request.user_name,
        request.text,
        is_group=request.is_group,
        images=request.images,
        has_image=bool(request.images),
        mentions=request.mentions,
        dedupe_key=request.dedupe_key,
        platform=request.platform,
        event_id=request.event_id,
        message_id=request.message_id,
        reply_to_message_id=request.reply_to_message_id,
        occurred_at=request.occurred_at,
        memory_images=list(zip(request.metadata.get("raw_image_paths", []), request.metadata.get("raw_images", []))),
        has_at_bot=mention_flags(bot, event)[0],
        sender_role=request.metadata.get("sender_role"),
        quote_snapshot=request.metadata.get("quote_snapshot"),
    )
    request.metadata.update({"history_recorded": True, "history_appended": True})
    has_at_bot, at_other = mention_flags(bot, event)
    request.metadata.update({"has_at_bot": has_at_bot, "at_other": at_other})
    await normal_scheduler.submit(chat_service, request, runtime)


def install(application: Application, authorizer: Authorizer) -> None:
    """显式安装群上下文和 AI 兜底路由，保留防抖与 Focus 调度。"""

    async def context_rule(context: HandlerContext) -> bool:
        return await authorizer.check("chat", context.event) and await _context_rule(context.bot, context.event)

    async def reply_rule(context: HandlerContext) -> bool:
        return await authorizer.check("chat", context.event) and await _ai_fallback_rule(context.bot, context.event)

    async def context(context: HandlerContext) -> None:
        await _handle_context(context.bot, context.event, application, authorizer)

    async def reply(context: HandlerContext) -> None:
        await _handle_ai_fallback(context.bot, context.event, context, application, authorizer)

    collect = on_message(name="context", display_name="记录群聊上下文", rule=context_rule, priority=98, block=False)
    answer = on_message(name="reply", display_name="AI回复", rule=reply_rule, priority=99)
    collect.handle()(context)
    answer.handle()(reply)
    bind_matchers(application, "chat", (collect.snapshot(), answer.snapshot()))
