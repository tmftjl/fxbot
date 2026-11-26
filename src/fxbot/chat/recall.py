"""统一处理原生撤回通知。"""

from __future__ import annotations

from ..core import Event, HandlerContext
from ..core.matcher import bind_matchers
from ..core.commands import on_event
from .message_adapter import session_id_for_event
from .memory.lifecycle import handle_recall
from ..core.application import Application


def recall_operator(event: Event) -> str:
    """撤回者：私聊只有作者本人能撤回自己的消息，作者就是会话对端；群聊由 operator_id 区分作者与代删者。"""
    if event.target.kind == "private":
        return event.user_id
    return str(event.metadata.get("operator_id") or "")


def install(application: Application) -> None:
    """撤回由协议层确定，不重新猜测平台事件字段。"""

    async def recall(context: HandlerContext) -> None:
        event = context.event
        if event.message_id is None:
            raise ValueError("撤回通知缺少消息 ID")
        await handle_recall(
            session_id_for_event(context.bot, event), event.message_id, operator=recall_operator(event)
        )

    notice = on_event("message_deleted", name="recall", display_name="同步撤回", priority=10, block=False)
    notice.handle()(recall)
    bind_matchers(application, "chat", (notice.snapshot(),))
