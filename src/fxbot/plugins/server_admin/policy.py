"""服务器命令的权限决策：黑名单之后，按模式和两阶段分类器决定执行还是确认。

结构对齐 Claude Code 的 auto 模式：本地分类器读取会话 transcript，先用快速阶段
决定放行，疑似危险时再用复核阶段判断；分类器不可用、超时或输出无法解析一律按
确认处理。
"""

from __future__ import annotations

import re
import sys
import json
import socket
import asyncio
from dataclasses import dataclass

from loguru import logger

from .config import permission_mode, confirmed_commands
from .classifier_prompt import SYSTEM_PROMPT

STAGE1_TIMEOUT = 8.0
STAGE2_TIMEOUT = 30.0
_STAGE1_MAX_TOKENS = 64
_STAGE2_MAX_TOKENS = 4096
_STAGE1_STOP = ["</block>"]
_MAX_HISTORY_MESSAGES = 40
_MAX_HISTORY_CHARS = 12000
_MAX_REQUEST_CHARS = 2000

_BLOCK = re.compile(r"<block>\s*(yes|no)\s*</block>", re.IGNORECASE)
_REASON = re.compile(r"<reason>(.*?)</reason>", re.IGNORECASE | re.DOTALL)
_CATEGORY = re.compile(r"<category>(.*?)</category>", re.IGNORECASE | re.DOTALL)
_MODE_PREFIX = re.compile(r"^[#＃]服务器模式[#＃]\s*")

_STAGE1_SUFFIX = "\nErr on the side of blocking. <block> immediately."
_STAGE2_SUFFIX = (
    "\nReview the classification process and follow it carefully, making sure you deny actions "
    "that should be blocked. As a reminder, explicit (not suggestive or implicit) user confirmation "
    "is required to override blocks. Use <thinking> before responding with <block>."
)


@dataclass(frozen=True)
class Decision:
    """一次命令提交的权限结论。"""

    action: str
    source: str
    reason: str = ""

    @property
    def auto_run(self) -> bool:
        """是否跳过确认直接执行。"""
        return self.action == "allow"


def rule_decision(command: str, *, mode: str | None = None) -> Decision | None:
    """只用模式和永久允许判断；没有结论时返回 None 交给分类器。"""
    value = command.strip()
    current = mode or permission_mode()
    if value in confirmed_commands():
        return Decision("allow", "rule", "已永久允许")
    if current == "bypass":
        return Decision("allow", "mode", "权限模式为全部放行")
    if current == "confirm":
        return Decision("ask", "rule", "逐条确认模式")
    return None


async def decide(*, session_id: str, command: str, cwd: str, request_text: str) -> Decision:
    """先走规则，规则没结论时问分类器；分类器异常或超时按确认处理。"""
    if not command.strip():
        return Decision("ask", "rule", "命令为空")
    ruled = rule_decision(command)
    if ruled is not None:
        return ruled
    try:
        return await _classify(session_id, command, cwd, request_text)
    except asyncio.TimeoutError:
        return Decision("ask", "classifier", "分类器超时")
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] 命令分类器失败，按确认处理")
        return Decision("ask", "classifier", "分类器不可用")


async def _classify(session_id: str, command: str, cwd: str, request_text: str) -> Decision:
    """两阶段判定：快速阶段只放行明确安全的命令，其余交给复核阶段。"""
    transcript = build_transcript(session_id, command, cwd, request_text)
    if len(transcript) > _MAX_HISTORY_CHARS:
        return Decision("ask", "classifier", "对话过长，分类器无法完整读取")
    if "User: " not in transcript:
        return Decision("ask", "classifier", "没有可对照的用户请求")
    system = _system_content(cwd)
    fast = await _ask(
        session_id,
        transcript + _STAGE1_SUFFIX,
        system=system,
        max_tokens=_STAGE1_MAX_TOKENS,
        timeout=STAGE1_TIMEOUT,
        stop=_STAGE1_STOP,
    )
    if parse_block(fast) == "no":
        return Decision("allow", "classifier", "快速分类放行")
    review = await _ask(
        session_id,
        transcript + _STAGE2_SUFFIX,
        system=system,
        max_tokens=_STAGE2_MAX_TOKENS,
        timeout=STAGE2_TIMEOUT,
    )
    verdict = parse_block(review)
    if verdict == "no":
        return Decision("allow", "classifier", "复核放行")
    category = parse_category(review)
    reason = parse_reason(review) or "复核未通过"
    label = f"[{category}] {reason}" if category else reason
    return Decision("ask", "classifier", f"分类器拦截：{label}"[:160])


async def _ask(
    session_id: str,
    content: str,
    *,
    system: str,
    max_tokens: int,
    timeout: float,
    stop: list[str] | None = None,
) -> str:
    """执行一次分类器请求；任何异常都由上层按确认处理。"""
    # 延迟导入：插件加载时 chat.providers 可能尚未初始化完成。
    from ...chat.providers.routing import TaskProvider, candidate_providers

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]
    # 与主对话使用同一个 Provider 和模型：按 reply 路由取候选，任务名只用于用量归集。
    async with TaskProvider(candidate_providers("reply", session_id), task="classifier") as provider:
        response = await asyncio.wait_for(
            provider.chat(
                messages=messages,
                temperature=0.0,
                max_tokens=max_tokens,
                stop=stop or None,
                thinking=False,
            ),
            timeout=timeout,
        )
    return str(response.content or "")


def _system_content(cwd: str) -> str:
    """系统提示词 + 本机运行环境，供分类器判断平台相关的命令。"""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    lines = [
        "",
        "## Session Context",
        f"- host: {host or 'unknown'}",
        f"- platform: {sys.platform}",
        f"- working directory: {cwd}",
        (
            "- interface: server_exec runs a shell command on this machine; "
            "there is no interactive approval inside the command"
        ),
    ]
    return SYSTEM_PROMPT + "\n".join(lines)


def build_transcript(session_id: str, command: str, cwd: str, request_text: str) -> str:
    """把会话历史压成分类器输入：用户/助手文本与 agent 的工具调用。"""
    lines: list[str] = []
    for message in _history(session_id)[-_MAX_HISTORY_MESSAGES:]:
        if message.get("_planner_internal") or message.get("_reference"):
            continue
        role = message.get("role")
        if role == "user":
            text = _one_line(_content_text(message.get("content")))
            if text:
                lines.append(f"User: {text}")
        elif role == "assistant":
            prose = _one_line(_content_text(message.get("content")))
            if prose:
                lines.append(f"Assistant: {prose}")
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                name = str((function or {}).get("name") or "").strip()
                if not name:
                    continue
                arguments = _compact_arguments((function or {}).get("arguments"))
                lines.append(f"{name} {arguments}")
    request = _one_line(_MODE_PREFIX.sub("", request_text))
    if not any(line == f"User: {request}" for line in lines) and request:
        lines.append(f"User: {request}")
    action = json.dumps({"command": command.strip(), "cwd": cwd}, ensure_ascii=False, separators=(",", ":"))
    lines.append(f"Bash {action}")
    text = "\n".join(lines)
    if len(text) > _MAX_HISTORY_CHARS:
        text = text[-_MAX_HISTORY_CHARS:]
    return f"<transcript>\n{text}\n</transcript>"


def _history(session_id: str) -> list[dict]:
    """读取当前会话历史；读取失败按空历史处理。"""
    try:
        from ...chat.session import default_session_store

        return list(default_session_store.get(session_id))
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] 读取会话历史失败")
        return []


def _content_text(content: object) -> str:
    """从消息 content 中提取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return ""


def _compact_arguments(arguments: object) -> str:
    """工具参数统一压成单行 JSON，避免伪造出新的 transcript 行。"""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return json.dumps(arguments, ensure_ascii=False)
    try:
        return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(arguments), ensure_ascii=False)


def _one_line(text: str) -> str:
    """把用户文本压成单行并截断，保证一行只表达一个角色。"""
    return " ".join(text.split())[:_MAX_REQUEST_CHARS]


def parse_block(text: str) -> str:
    """解析 <block>yes|no</block>；没有合法标签返回空串。"""
    found = _BLOCK.search(text)
    return found.group(1).lower() if found else ""


def parse_reason(text: str) -> str:
    """取 <reason> 内容并压成单行。"""
    found = _REASON.search(text)
    return _one_line(found.group(1))[:120] if found else ""


def parse_category(text: str) -> str:
    """取 <category> 命中的拦截规则名。"""
    found = _CATEGORY.search(text)
    return _one_line(found.group(1))[:60] if found else ""
