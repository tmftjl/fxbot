"""统一处理原生撤回通知。"""

from __future__ import annotations

from ..core import HandlerContext
from .message_adapter import session_id_for_event
from .memory.lifecycle import handle_recall
from ..core.application import Application


def install(application: Application) -> None:
    """撤回由协议层确定，不重新猜测平台事件字段。"""

    async def recall(context: HandlerContext) -> None:
        event = context.event
        if event.message_id is None:
            raise ValueError("撤回通知缺少消息 ID")
        await handle_recall(
            session_id_for_event(context.bot, event), event.message_id, operator=event.metadata.get("operator_id")
        )

    application.router.event("chat:recall", "message_deleted", recall, priority=10, block=False)
