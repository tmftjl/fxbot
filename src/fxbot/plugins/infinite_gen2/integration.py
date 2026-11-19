"""通过可修改模型请求注入独立系统段，不改人格、历史或权限。"""

from __future__ import annotations

from pathlib import Path

from .config import is_enabled, direct_llm_enabled
from ...chat.extensions import LLMRequest, ChatContext, ChatExtension, SessionPolicy

PROMPT_TEXT = (Path(__file__).parent / "resources" / "infinite_gen2.prompt").read_text(encoding="utf-8").strip()
_PROMPT_MARKER = "[无限二代提示词]\n"
_extension = ChatExtension("infinite_gen2")


def prepare_request(context: ChatContext, request: LLMRequest) -> None:
    """普通规划、Focus、回复和直聊均注入；循环及续写只保留最新的一段。"""
    del context
    if request.phase not in {"reply", "planner"}:
        return
    request.messages[:] = [
        message
        for message in request.messages
        if not (message.get("role") == "system" and str(message.get("content", "")).startswith(_PROMPT_MARKER))
    ]
    if not is_enabled():
        return
    position = next(
        (index for index, message in enumerate(request.messages) if message.get("role") != "system"),
        len(request.messages),
    )
    request.messages.insert(position, {"role": "system", "content": f"{_PROMPT_MARKER}{PROMPT_TEXT}"})


def direct_chat_policy(session_id: str) -> SessionPolicy | None:
    """开关打开时声明纯人格直聊，核心按会话策略统一处理。"""
    del session_id
    return SessionPolicy(direct_reply=True) if direct_llm_enabled() else None


def install() -> None:
    """同名注册保证重复加载不叠加提示词。"""
    _extension.on("llm_request", prepare_request)
    _extension.on("session_policy", direct_chat_policy)


install()
