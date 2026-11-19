"""开放的可修改执行 hook、拒绝型守卫与失败隔离的生命周期通知。"""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
from types import MappingProxyType
from typing import Any, Literal, get_args
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Collection, MutableMapping

from loguru import logger

from .hook_registry import Registration, CallbackRegistry, ordered_callbacks, register_callback

HOOK_TIMEOUT_SECONDS = 5.0
_IMMUTABLE_SCALARS = frozenset({str, int, float, bool, bytes, type(None)})
_hooks: dict[str, MutableMapping[str, Callable[..., Any]]] = {}
HookKind = Literal[
    "message",
    "gate",
    "chat_request",
    "llm_request",
    "llm_response",
    "tool_start",
    "tool_end",
    "reply",
    "after_send",
    "chat_done",
    "request_guard",
    "tool_guard",
    "send_guard",
    "observer",
    "stage_guard",
    "ingress_guard",
    "session_resolver",
    "session_policy",
]
_KINDS = frozenset(get_args(HookKind))
_active_hooks: ContextVar[frozenset[tuple[str, int]]] = ContextVar("chat_active_hooks", default=frozenset())


async def dispatch(kind: str, context: Any, *args: Any) -> None:
    """工作对象直接传递；执行入口失败闭锁，普通扩展记录错误后继续，取消始终传播。"""
    active = _active_hooks.get()
    slot = (kind, id(context))
    if slot in active:
        return
    token = _active_hooks.set(active | {slot})
    try:
        for name, callback in ordered_callbacks(_hooks.get(kind, {})):
            try:
                value = callback(context, *args)
                if inspect.isawaitable(value):
                    await value
                elif value is not None:
                    raise TypeError("执行 hook 必须返回 None，通过上下文设置结果")
            except Exception as exc:
                logger.opt(exception=True).warning("[ChatHook] 扩展失败: {}/{}", kind, name)
                if kind in {"message", "chat_request"}:
                    raise ChatHookError(f"{kind} 扩展失败: {name}") from exc
            if kind in {"message", "chat_request"} and context.is_stopped():
                break
    finally:
        _active_hooks.reset(token)


def _freeze(value: Any) -> tuple[Any, bool]:
    """冻结常见容器并标记是否可安全共享，未知对象仍保留逐订阅者深拷贝。"""
    if type(value) in _IMMUTABLE_SCALARS:
        return value, True
    if isinstance(value, dict):
        frozen = {}
        shared = True
        for key, item in value.items():
            frozen[key], item_shared = _freeze(item)
            shared = shared and item_shared and type(key) in _IMMUTABLE_SCALARS
        return MappingProxyType(frozen), shared
    if isinstance(value, (list, tuple)):
        pairs = [_freeze(item) for item in value]
        return tuple(item for item, _ in pairs), all(shared for _, shared in pairs)
    if isinstance(value, (set, frozenset)):
        pairs = [_freeze(item) for item in value]
        return frozenset(item for item, _ in pairs), all(shared for _, shared in pairs)
    return deepcopy(value), False


class _DetailsSnapshot:
    """一次派发只冻结一次纯数据；含可变对象时维持各订阅者独立快照。"""

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details
        self.shared = False
        self.value: Any = None

    def get(self) -> Any:
        """按需准备快照，无匹配订阅者时不遍历载荷。"""
        if not self.shared:
            self.value, self.shared = _freeze(self.details)
        return self.value


@dataclass(frozen=True)
class ChatHookContext:
    """不可变的调用身份，不包含可写请求、事件、发送目标或运行时权限对象。"""

    session_id: str
    user_id: str = ""
    group_id: str | None = None
    platform: str = ""
    turn_id: str = ""
    phase: str = "reply"
    epoch: int | None = None
    source: str = "message"


class ChatHookError(RuntimeError):
    """阻止有风险的回调失败继续生成或发送。"""


class UnsupportedChatHook(ImportError):
    """插件依赖已不存在的聊天扩展 API；按不可加载插件处理。"""


def has_chat_hooks(*kinds: str) -> bool:
    """无订阅时跳过额外身份解析，不改变原路由的廉价匹配路径。"""
    return any(_hooks.get(kind) for kind in kinds)


def hook_context(request: Any, *, phase: str | None = None) -> ChatHookContext:
    """从真实请求提取身份，不向插件传递可改写的原对象。"""
    return ChatHookContext(
        session_id=request.session_id,
        user_id=request.user_id,
        group_id=request.group_id,
        platform=request.platform,
        turn_id=request.turn_id,
        phase=phase
        or (
            "planner" if request.metadata.get("planner_loop") and not request.metadata.get("planner_reply") else "reply"
        ),
        epoch=request.accepted_epoch,
        source=str(request.metadata.get("_extension_owner") or "message"),
    )


def register_chat_hook(
    kind: HookKind,
    name: str,
    callback: Callable[..., Any],
    *,
    priority: int = 100,
    events: Collection[str] | None = None,
) -> Callable[[], None]:
    """按类型和插件名称注册，稳定排序、同名替换并返回精确注销句柄。"""
    if events is not None:
        if (
            kind != "observer"
            or not isinstance(events, Collection)
            or isinstance(events, str)
            or any(not isinstance(event, str) or not event.strip() for event in events)
        ):
            raise ValueError("事件筛选只支持通知且需要事件名称集合")
        events = frozenset(events)
    if kind in {"session_resolver", "session_policy"}:
        from .session_hooks import register_session_policy, register_session_resolver

        register = register_session_resolver if kind == "session_resolver" else register_session_policy
        return register(name, callback, priority=priority)
    if kind not in _KINDS:
        raise UnsupportedChatHook(f"聊天 hook {kind!r} 不存在，请迁移到开放式聊天扩展 API")
    return register_callback(
        _hooks.setdefault(kind, CallbackRegistry()),
        name,
        callback,
        priority=priority,
        events=events,
    )


async def _call(callback: Callable[..., Any], *args: Any) -> Any:
    """异步回调限时执行，同步回调必须由插件保证不阻塞。"""
    value = callback(*args)
    return await asyncio.wait_for(value, HOOK_TIMEOUT_SECONDS) if inspect.isawaitable(value) else value


async def guard(hook_kind: str, context: ChatHookContext, **details: Any) -> bool:
    """拒绝优先，None 表示不干预；异常、超时或非法返回均拒绝。"""
    if hook_kind not in {"request_guard", "tool_guard", "send_guard", "stage_guard", "ingress_guard"}:
        raise ValueError("无效的守卫类型")
    callbacks = ordered_callbacks(_hooks.get(hook_kind, {}))
    if not callbacks:
        return True
    snapshot = _DetailsSnapshot(details)
    for name, callback in callbacks:
        try:
            value = await _call(callback, context, snapshot.get())
            if value is False:
                return False
            if value is not None and value is not True:
                raise TypeError("守卫只允许返回 bool 或 None")
        except Exception:
            logger.opt(exception=True).warning(f"[ChatHook] 守卫失败: {hook_kind}/{name}")
            return False
    return True


async def notify(event: str, context: ChatHookContext, **details: Any) -> None:
    """按事件筛选通知，共享不可变快照，可变对象隔离，失败不影响核心结果。"""
    callbacks = ordered_callbacks(_hooks.get("observer", {}))
    if not callbacks:
        return
    snapshot = _DetailsSnapshot(details)
    for name, callback in callbacks:
        if isinstance(callback, Registration) and callback.events is not None and event not in callback.events:
            continue
        try:
            await _call(callback, event, context, snapshot.get())
        except Exception:
            logger.opt(exception=True).warning(f"[ChatHook] 通知失败: {event}/{name}")
