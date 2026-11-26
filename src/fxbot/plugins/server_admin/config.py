"""服务器运维控制台配置与命令黑名单。"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from ...config import get_manager
from .ui_schema import DEFAULTS
from ...utils.paths import data_dir

REG = get_manager().register("server_admin", DEFAULTS, clean_extra=True)


MODES = ("confirm", "auto", "bypass")
MAX_CONFIRMED = 200


def confirmed_commands() -> list[str]:
    """人工选择「永久允许」后记住的完整命令；配置不可读时返回空表。"""
    rules = REG.load()
    if REG._last_error is not None:
        return []
    return [str(item).strip() for item in rules.get("confirmed_commands") or [] if str(item).strip()]


def remember_command(command: str) -> bool:
    """把人工确认过的完整命令写入持久清单，下次直接执行；返回是否已记住。"""
    text = command.strip()
    if not text:
        return False
    rules = REG.load()
    if REG._last_error is not None:
        return False
    current = [str(item).strip() for item in rules.get("confirmed_commands") or [] if str(item).strip()]
    if text in current:
        return True
    rules["confirmed_commands"] = [*current, text][-MAX_CONFIRMED:]
    try:
        REG.save(rules)
    except Exception:
        logger.opt(exception=True).error("[ServerAdmin] 保存永久允许命令失败")
        return False
    return True


def permission_mode() -> str:
    """权限模式：confirm 逐条确认、auto 跟随请求判断、bypass 全部放行；配置不可读按逐条确认。"""
    rules = REG.load()
    if REG._last_error is not None:
        return "confirm"
    mode = str(rules.get("permission_mode") or "").strip().lower()
    return mode if mode in MODES else "confirm"


def skip_confirmation() -> bool:
    """当前是否处于全部放行；保留给旧的调用点。"""
    return permission_mode() == "bypass"


_POLICY_KEYS = ("allow_sharing", "allow_debounce", "allow_typing", "allow_split")


def policy_switches() -> dict[str, bool]:
    """服务器会话的能力开关；配置不可读时沿用默认值。"""
    rules = REG.load()
    if REG._last_error is not None:
        rules = {}
    return {key: rules.get(key, DEFAULTS[key]) is True for key in _POLICY_KEYS}


def persona_text() -> str:
    """控制台填写的运维人格；留空或配置不可读时使用插件内置版本。"""
    rules = REG.load()
    if REG._last_error is not None:
        return ""
    return str(rules.get("persona") or "").strip()


def blocked_reason(command: str) -> str:
    """命中禁用规则时返回该规则；配置读不出来时按拒绝处理。"""
    rules = REG.load()
    if REG._last_error is not None:
        return "命令黑名单配置不可用"
    for pattern in rules.get("blocked_commands") or []:
        rule = str(pattern).strip()
        if not rule:
            continue
        try:
            if re.search(rule, command, re.IGNORECASE | re.MULTILINE):
                return rule
        except re.error:
            logger.warning(f"[ServerAdmin] 无效的命令黑名单规则已忽略: {rule}")
    return ""


CC_ENGINE_COMMANDS: dict[str, tuple[str, ...]] = {
    "claude": ("npx", "-y", "@agentclientprotocol/claude-agent-acp@0.112.0"),
    "codex": ("npx", "-y", "@agentclientprotocol/codex-acp@2.0.1"),
}
CC_ENGINES = tuple(CC_ENGINE_COMMANDS)


def cc_agent_command(engine: str = "claude") -> list[str]:
    """ACP 适配器启动命令，按引擎固定版本。"""
    command = CC_ENGINE_COMMANDS.get(engine) or CC_ENGINE_COMMANDS["claude"]
    return list(command)


def cc_workdir(key: str) -> Path:
    """按用户派生的独立工作目录，固定放在项目数据目录下。"""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", key)[:60] or "user"
    workdir = (data_dir("claude_code") / "workdir" / safe).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def cc_idle_seconds() -> int:
    """空闲回收秒数，最少 60。"""
    rules = REG.load()
    value = DEFAULTS["cc_idle_seconds"] if REG._last_error is not None else rules.get("cc_idle_seconds")
    if isinstance(value, bool) or not isinstance(value, int):
        return int(DEFAULTS["cc_idle_seconds"])
    return max(60, value)


def cc_show_thinking() -> bool:
    """是否回发 agent 的思考片段。"""
    rules = REG.load()
    return REG._last_error is None and rules.get("cc_show_thinking") is True


def cc_keep_context() -> bool:
    """空闲回收时是否保留 ACP 会话上下文；默认关闭。"""
    rules = REG.load()
    return REG._last_error is None and rules.get("cc_keep_context") is True


def cc_permission_mode() -> str:
    """Claude Code 工具权限：ask 或 allow；配置不可读按 ask。"""
    rules = REG.load()
    if REG._last_error is not None:
        return "ask"
    value = str(rules.get("cc_permission_mode") or "").strip().lower()
    return value if value in {"ask", "allow"} else "ask"


def cc_output_format() -> str:
    """Claude Code 输出格式：image / text / auto。"""
    rules = REG.load()
    if REG._last_error is not None:
        return "image"
    value = str(rules.get("cc_output_format") or "").strip().lower()
    return value if value in {"image", "text", "auto"} else "image"


def cc_render_scale() -> int:
    """渲染像素密度 1~3。"""
    rules = REG.load()
    value = DEFAULTS["cc_render_scale"] if REG._last_error is not None else rules.get("cc_render_scale")
    if isinstance(value, bool) or not isinstance(value, int):
        return int(DEFAULTS["cc_render_scale"])
    return min(3, max(1, value))


def cc_tool_display() -> str:
    """工具调用显示档位：off / brief / full。"""
    rules = REG.load()
    if REG._last_error is not None:
        return "off"
    value = str(rules.get("cc_tool_display") or "").strip().lower()
    return value if value in {"off", "brief", "full"} else "off"


def cc_approve_timeout() -> int:
    """权限请求等待人工回复的秒数，10~3600。"""
    rules = REG.load()
    value = DEFAULTS["cc_approve_timeout"] if REG._last_error is not None else rules.get("cc_approve_timeout")
    if isinstance(value, bool) or not isinstance(value, int):
        return int(DEFAULTS["cc_approve_timeout"])
    return min(3600, max(10, value))


def cc_busy_behavior() -> str:
    """忙时行为：queue 排队或 reject 拒绝。"""
    rules = REG.load()
    if REG._last_error is not None:
        return "queue"
    value = str(rules.get("cc_busy_behavior") or "").strip().lower()
    return value if value in {"queue", "reject"} else "queue"


def cc_engine(key: str) -> str:
    """该用户当前选择的引擎，未知值回退 claude。"""
    from . import cc_store

    engine = cc_store.get_engine(key)
    return engine if engine in CC_ENGINE_COMMANDS else "claude"


def cc_session_scope() -> str:
    """Claude Code 会话范围：single 全平台共用一个，user 按平台用户隔离。"""
    rules = REG.load()
    if REG._last_error is not None:
        return "user"
    value = str(rules.get("cc_session_scope") or "").strip().lower()
    return value if value in {"single", "user"} else "user"
