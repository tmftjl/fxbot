"""在线 Bot 快照对比、掉线判定与超管私聊提醒。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from .config import get_config
from ...core import Message, SessionTarget
from ...core.errors import ActionFailed

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
    """逐条投递给超级用户；任一投递成功即视为本轮已送达。"""
    system = services.configuration.get_system().get("system") or {}
    entries = [str(item).strip() for item in system.get("superusers") or [] if str(item).strip()]
    delivered = False
    for entry in entries:
        for bot in _candidates(bots, entry):
            target = SessionTarget(bot.platform, bot.bot_id, "private", entry)
            try:
                await bot.send(target, Message.text(text))
            except _DELIVERY_ERRORS:
                continue
            delivered = True
            break
    if not delivered:
        logger.warning("[BotMonitor] 提醒未能投递，下一轮重试: {}", text.splitlines()[-1])
    return delivered


async def run_tick(services: Any, state: dict[str, Any], *, now: float | None = None) -> None:
    """对比两次快照；首次只建立基线，维护窗口内的抖动由宽限期吸收。"""
    config = get_config()
    if not config["enabled"]:
        return
    bots = tuple(services.application.bots)
    current = {_key(bot): bot for bot in bots}
    moment = time.monotonic() if now is None else now
    wall = time.time()

    if not state.get("initialized"):
        state["online"] = set(current)
        state["offline"] = {}
        state["initialized"] = True
        return

    online: set[str] = state["online"]
    offline: dict[str, dict[str, float]] = state["offline"]

    for key in set(current) & set(offline):
        info = offline.pop(key)
        online.add(key)
        # 只有真的发过掉线提醒才补一条恢复；宽限内的重连抖动完全静默。
        if config["notify_recovery"] and info.get("alerted"):
            await _notify(
                services,
                bots,
                f"【Bot 恢复】{key} 已重新上线，离线时长 {_duration(moment - info['since'])}",
            )

    for key in set(current) - online:
        online.add(key)

    for key in online - set(current):
        online.discard(key)
        offline.setdefault(key, {"since": moment, "since_wall": wall, "alerted": 0.0})

    for key, info in list(offline.items()):
        if key in current:
            continue
        elapsed = moment - info["since"]
        if elapsed < config["offline_grace_seconds"]:
            continue
        repeat = float(config["repeat_minutes"]) * 60
        last = info.get("alerted", 0.0)
        if last and (repeat <= 0 or moment - last < repeat):
            continue
        text = "\n".join(
            [
                "【Bot 掉线】",
                key,
                f"离线时长：{_duration(elapsed)}",
                f"开始时间：{_clock(info['since_wall'])}",
            ]
        )
        if await _notify(services, bots, text):
            info["alerted"] = moment
