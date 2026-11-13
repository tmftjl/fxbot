"""插件可注册会话解析和隔离策略；核心不识别任何业务命名空间。"""

from __future__ import annotations

from inspect import isawaitable
from dataclasses import dataclass
from urllib.parse import quote, unquote
from collections.abc import Callable

from loguru import logger

from ..core import Event, BotClient, SessionTarget
from .hook_registry import CallbackRegistry, ordered_callbacks, register_callback

SessionResolver = Callable[[BotClient, Event, str], str | None]


@dataclass(frozen=True)
class SessionPolicy:
    """会话能力与响应节奏，多个插件的限制取交集；allow_rest_time、direct_reply 为放行项，任一插件声明即生效。"""

    allow_focus: bool = True
    allow_sharing: bool = True
    allow_debounce: bool = True
    allow_typing: bool = True
    allow_split: bool = True
    allow_typo: bool = True
    allow_rest_time: bool = False
    direct_reply: bool = False


PolicyResolver = Callable[[str], SessionPolicy | None]
_resolvers = CallbackRegistry()
_policies = CallbackRegistry()
_EVENT_ATTR = "_fxbot_resolved_sessions"


def session_target(session_id: str) -> SessionTarget:
    """解析规范聊天会话；中间隔离域不改变连接和真实目标。"""
    parts = session_id.split(":")
    if len(parts) < 4 or any(not part or quote(unquote(part), safe="") != part for part in parts):
        raise ValueError("聊天会话不是规范编码")
    return SessionTarget.from_key(":".join([*parts[:2], *parts[-2:]]))


def register_session_resolver(name: str, resolver: SessionResolver, *, priority: int = 100) -> Callable[[], None]:
    """按注册顺序添加同步解析器，同名替换不改优先级，返回精确注销句柄。"""
    return register_callback(_resolvers, name, resolver, priority=priority, synchronous=True)


def register_session_policy(name: str, resolver: PolicyResolver, *, priority: int = 100) -> Callable[[], None]:
    """注册同步策略解析器，策略每次使用重新读取以支持撤权和配置热更新。"""
    return register_callback(_policies, name, resolver, priority=priority, synchronous=True)


def session_policy(session_id: str) -> SessionPolicy:
    """合并所有限制，策略异常时关闭受控能力，禁止降级到无限制。"""
    result = SessionPolicy()
    for name, resolver in ordered_callbacks(_policies):
        try:
            policy = resolver(session_id)
            if isawaitable(policy):
                close = getattr(policy, "close", None)
                if callable(close):
                    close()
                raise TypeError("会话策略必须同步返回")
            if policy is None:
                continue
            if not isinstance(policy, SessionPolicy) or any(
                type(value) is not bool
                for value in (
                    policy.allow_focus,
                    policy.allow_sharing,
                    policy.allow_debounce,
                    policy.allow_typing,
                    policy.allow_split,
                    policy.allow_typo,
                    policy.allow_rest_time,
                    policy.direct_reply,
                )
            ):
                raise TypeError("会话策略必须返回 SessionPolicy 或 None")
            result = SessionPolicy(
                result.allow_focus and policy.allow_focus,
                result.allow_sharing and policy.allow_sharing,
                result.allow_debounce and policy.allow_debounce,
                result.allow_typing and policy.allow_typing,
                result.allow_split and policy.allow_split,
                result.allow_typo and policy.allow_typo,
                result.allow_rest_time or policy.allow_rest_time,
                result.direct_reply or policy.direct_reply,
            )
        except Exception:
            logger.opt(exception=True).error(f"[ChatSession] 会话策略失败: {name}")
            return SessionPolicy(False, False, False, False, False, False)
    return result


def direct_reply_mode(session_id: str) -> bool:
    """任一插件为该会话声明纯人格直聊时生效。"""
    return session_policy(session_id).direct_reply


def can_share_sessions(source: str, target: str) -> bool:
    """跨会话共享要求两端均允许，自身会话不受共享开关影响。"""
    return source == target or (session_policy(source).allow_sharing and session_policy(target).allow_sharing)


def resolve_session_id(bot: BotClient, event: Event, base_session_id: str) -> str:
    """首个非空覆盖生效，结果固定在事件对象上；解析失败拒绝处理而非退回普通会话。"""
    cached = event.metadata.setdefault(_EVENT_ATTR, {})
    if base_session_id in cached:
        resolved = cached[base_session_id]
        if resolved is None:
            raise RuntimeError("会话解析已失败或发生递归")
        return resolved
    cached[base_session_id] = None
    resolved = base_session_id
    base = base_session_id.split(":")
    for _, resolver in ordered_callbacks(_resolvers):
        candidate = resolver(bot, event, base_session_id)
        if isawaitable(candidate):
            if hasattr(candidate, "close"):
                candidate.close()
            raise TypeError("会话解析器必须同步返回")
        if candidate is None or candidate == "":
            continue
        if not isinstance(candidate, str):
            raise TypeError("会话解析器必须返回字符串或 None")
        parts = candidate.split(":")
        # 插件可添加隔离域，但不能更改连接身份、会话类型或真实收件人。
        if (
            len(parts) < 4
            or parts[:2] != base[:2]
            or parts[-2:] != base[-2:]
            or any(not part or any(char.isspace() for char in part) for part in parts)
            or any(quote(unquote(part), safe="") != part for part in parts)
        ):
            raise ValueError("会话解析器只能在连接与目标之间添加隔离域")
        resolved = candidate
        break
    cached[base_session_id] = resolved
    return resolved
