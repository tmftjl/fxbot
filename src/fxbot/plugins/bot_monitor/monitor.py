"""Bot 监控：掉线与假在线共用一套判据，连续失败后重启容器。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from .liveness import discover_containers, restart_container
from ...core import Message, SessionTarget
from ...core.errors import ActionFailed

INTERVAL_SECONDS = 300.0
"""掉线快照与假在线探活共用的检测间隔。"""

FAIL_THRESHOLD = 3
"""连续失败达到该次数才处置，中间任意一次成功即清零。"""

PROBE_TIMEOUT = 10.0
"""单次探活的响应超时。"""

ACTION_COOLDOWN_SECONDS = 900.0
"""同一 Bot 两次处置之间的最小间隔。"""

SHANGHAI = ZoneInfo("Asia/Shanghai")
_HEX = set("0123456789abcdefABCDEF")
_DELIVERY_ERRORS = (ActionFailed, ConnectionError, TimeoutError, OSError, ValueError)


def _key(bot: Any) -> str:
    return f"{bot.platform}:{bot.bot_id}"


def _duration(seconds: float) -> str:
    total = max(1, int(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def _clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, SHANGHAI).strftime("%m-%d %H:%M")


def _candidates(bots: tuple[Any, ...], entry: str) -> list[Any]:
    """按身份形态选平台：纯数字走 OneBot，32 位十六进制走 QQ 官方。"""
    if entry.isdigit() and len(entry) <= 12:
        return [bot for bot in bots if bot.platform.startswith("onebot")]
    if len(entry) == 32 and set(entry) <= _HEX:
        return [bot for bot in bots if bot.platform == "qq"]
    return []


async def _notify(services: Any, bots: tuple[Any, ...], text: str) -> bool:
    """只用一个 Bot 发一条提醒，首个投递成功即停。"""
    system = services.configuration.get_system().get("system") or {}
    entries = [str(item).strip() for item in system.get("superusers") or [] if str(item).strip()]
    for entry in entries:
        for bot in _candidates(bots, entry):
            target = SessionTarget(bot.platform, bot.bot_id, "private", entry)
            try:
                await bot.send(target, Message.text(text))
            except _DELIVERY_ERRORS:
                continue
            return True
    logger.warning("[BotMonitor] 提醒未能投递，下一轮重试: {}", text.splitlines()[-1])
    return False


def _probe_action(bot: Any) -> str | None:
    """只有 OneBot 连接能主动探活；其它平台只看在线快照。"""
    return "get_login_info" if str(getattr(bot, "platform", "")).startswith("onebot") else None


def _allows_action(state: dict[str, Any], key: str, moment: float) -> bool:
    """处置受冷却约束，避免网络抖动时反复重启。"""
    history = [item for item in state.setdefault("actions", {}).get(key, []) if moment - item < ACTION_COOLDOWN_SECONDS]
    state["actions"][key] = history
    return not history


async def _act(services: Any, state: dict[str, Any], key: str, bot: Any, moment: float) -> None:
    """能定位到容器就重启，否则只提醒；两种情况共用同一套节流。"""
    bots = tuple(services.application.bots)
    bot_id = str(bot.bot_id) if bot is not None else key.split(":", 1)[-1]
    containers = await discover_containers(state, moment)
    name = containers.get(bot_id)
    if name is not None:
        restarted = await restart_container(name)
        text = "\n".join(
            [
                "【Bot 异常】",
                key,
                f"连续 {FAIL_THRESHOLD} 次检测失败",
                f"已重启容器 {name}" if restarted else f"重启容器 {name} 失败，请人工检查",
            ]
        )
        if restarted:
            state["actions"][key].append(moment)
    else:
        text = "\n".join(
            [
                "【Bot 异常】",
                key,
                f"连续 {FAIL_THRESHOLD} 次检测失败",
                "未找到对应容器，仅提醒",
            ]
        )
        state["actions"][key].append(moment)
    if await _notify(services, bots, text):
        state.setdefault("alerted", set()).add(key)


async def run_tick(services: Any, state: dict[str, Any], *, now: float | None = None) -> None:
    """一轮同时判定掉线与假在线：连续失败达到阈值后按节流处置。"""
    moment = time.monotonic() if now is None else now
    current = {_key(bot): bot for bot in services.application.bots}
    known: set[str] = state.setdefault("known", set())
    known.update(current)
    fails: dict[str, int] = state.setdefault("fails", {})
    alerted: set[str] = state.setdefault("alerted", set())

    for key in sorted(known):
        bot = current.get(key)
        action = _probe_action(bot) if bot is not None else None
        healthy = bot is not None
        if action is not None:
            try:
                await bot.call(action, timeout=PROBE_TIMEOUT)
            except Exception:
                healthy = False
        fails[key] = 0 if healthy else fails.get(key, 0) + 1

    for key in sorted(alerted & {item for item, count in fails.items() if count == 0} & set(current)):
        alerted.discard(key)
        logger.info("[BotMonitor] {} 已恢复", key)
        await _notify(services, tuple(services.application.bots), f"【Bot 恢复】{key} 已恢复响应")

    for key in sorted(known):
        if fails.get(key, 0) < FAIL_THRESHOLD:
            continue
        if not _allows_action(state, key, moment):
            logger.warning("[BotMonitor] {} 连续失败，但处于冷却期", key)
            continue
        await _act(services, state, key, current.get(key), moment)
        fails[key] = 0
