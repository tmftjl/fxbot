"""群聊日报的拉取、分析、渲染与持久化编排。"""

from __future__ import annotations

import re
import json
import base64
import asyncio
import hashlib
from io import BytesIO
from uuid import uuid4
from typing import Any
from datetime import date, timedelta
from dataclasses import dataclass

import httpx
from PIL import Image
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from fxbot.core.errors import ActionFailed

from ...core import BotClient, Capability, SessionTarget, UnsupportedCapability
from .config import get_config, is_enabled, group_allowed, resolve_persona
from .models import GroupAnalysisReport, utc_now_naive
from .history import HistoryWindow, build_stats, build_user_stats, collect_messages, build_analysis_messages
from .renderer import render_report
from .analyzers import analyze
from ...utils.tz import now
from ...chat.media import load_image_bytes
from ...utils.paths import data_dir

_RUNNING: set[str] = set()
_AVATARS: dict[str, str] = {}
_MENTION = re.compile(r"\[([A-Za-z0-9_-]{1,128})\]")

REASON_TEXT = {
    "disabled": "群聊日报功能未启用",
    "group_not_allowed": "本群未启用群聊日报",
    "busy": "该群正在生成日报，请稍后再试",
    "unsupported": "当前平台不支持拉取群历史消息，无法生成日报",
    "fetch_failed": "拉取群消息失败，请稍后再试",
    "too_few": "这段时间的消息太少，暂时无法生成日报",
    "failed": "生成日报失败，请稍后再试",
}


@dataclass(slots=True)
class ReportResult:
    ok: bool
    image: bytes = b""
    reason: str = ""


def is_running(group_id: str) -> bool:
    return group_id in _RUNNING


def manual_label(days: int) -> str:
    return "最近 24 小时" if days <= 1 else f"最近 {days} 天"


def daily_period() -> tuple[int, str, str]:
    """返回今天零点（窗口终点）、前一天标签和日报日期。"""
    today_start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today_start - timedelta(days=1)
    date = yesterday.strftime("%Y-%m-%d")
    return int(today_start.timestamp()), f"{date} 全天", date


async def group_name_for(bot: BotClient, target: SessionTarget) -> str:
    if Capability.TARGET_LOOKUP not in bot.capabilities:
        return ""
    try:
        return await bot.capabilities.require(Capability.TARGET_LOOKUP).target_name(target)
    except (UnsupportedCapability, ActionFailed, TimeoutError, OSError):
        logger.opt(exception=True).debug("群聊日报名称查询暂不可用")
        return ""


def _replace_mentions(text: str, names: dict[str, str]) -> str:
    return _MENTION.sub(lambda match: names.get(match.group(1), match.group(0)), text or "")


async def _avatar_data(url: str) -> str:
    if not url:
        return ""
    if url not in _AVATARS:
        try:
            content = await load_image_bytes(url, limit=2 * 1024 * 1024)
            with Image.open(BytesIO(content)) as image:
                output = BytesIO()
                image.save(output, format="PNG")
            data = f"data:image/png;base64,{base64.b64encode(output.getvalue()).decode('ascii')}"
        except (httpx.HTTPError, OSError, ValueError):
            logger.opt(exception=True).debug("群聊日报头像暂不可用")
            return ""
        if len(_AVATARS) >= 256:
            _AVATARS.pop(next(iter(_AVATARS)))
        _AVATARS[url] = data
    return _AVATARS[url]


async def _attach_avatars(bot: BotClient, analysis: dict[str, Any]) -> None:
    if Capability.AVATAR not in bot.capabilities:
        return
    items = [*analysis["titles"], *analysis["quotes"]]
    user_urls: dict[str, str] = {}
    for item in items:
        user_id = str(item.get("user_id") or "")
        if not user_id:
            continue
        url = bot.capabilities.require(Capability.AVATAR).avatar_url(user_id)
        if url:
            user_urls[user_id] = str(url)
    if not user_urls:
        return
    urls = list(dict.fromkeys(user_urls.values()))
    results = await asyncio.gather(*(_avatar_data(url) for url in urls), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    fetched = dict(zip(urls, results, strict=True))
    for item in items:
        url = user_urls.get(str(item.get("user_id") or ""))
        if url and fetched.get(url):
            item["avatar_url"] = fetched[url]


def _resolve_names(analysis: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    names = {str(msg["user_id"]): str(msg.get("sender") or "") for msg in messages if msg.get("user_id")}
    for topic in analysis["topics"]:
        topic["summary"] = _replace_mentions(topic.get("summary") or "", names)
    for quote in analysis["quotes"]:
        quote["reason"] = _replace_mentions(quote.get("reason") or "", names)
    for title in analysis["titles"]:
        title["reason"] = _replace_mentions(title.get("reason") or "", names)


async def generate(
    bot: BotClient,
    target: SessionTarget,
    *,
    days: int,
    end: int,
    label: str,
    report_date: str,
    group_name: str = "",
    session_id: str = "",
) -> ReportResult:
    """同一群同时只跑一份；失败只返回预定义原因，不向用户泄露异常文本。"""
    if target.kind != "group" or (target.platform, target.bot_id) != (bot.platform, bot.bot_id):
        raise ValueError("日报目标必须属于当前 Bot 的群会话")
    if date.fromisoformat(report_date).isoformat() != report_date:
        raise ValueError("日报日期必须为规范 ISO 日期")
    group_id = target.key()
    if not is_enabled():
        return ReportResult(False, reason="disabled")
    if not group_allowed(group_id):
        return ReportResult(False, reason="group_not_allowed")
    if group_id in _RUNNING:
        return ReportResult(False, reason="busy")
    if Capability.HISTORY not in bot.capabilities:
        return ReportResult(False, reason="unsupported")
    _RUNNING.add(group_id)
    try:
        config = get_config()
        basic = config["basic"]
        window = HistoryWindow(start=end - days * 86400, end=end, label=label, days=days)
        try:
            messages = await collect_messages(
                bot,
                target,
                window,
                limit=basic["max_messages"],
                filter_self=basic["filter_self"],
            )
        except UnsupportedCapability:
            return ReportResult(False, reason="unsupported")
        except (ActionFailed, TimeoutError, OSError):
            logger.opt(exception=True).warning(f"[group_analysis] 群 {group_id} 拉取历史消息失败")
            return ReportResult(False, reason="fetch_failed")
        if len(messages) < basic["min_messages"]:
            return ReportResult(False, reason="too_few")
        features = config["analysis_features"]
        stats = build_stats(messages, window)
        analysis_messages = build_analysis_messages(messages, {bot.bot_id})
        users = build_user_stats(messages, features["max_user_titles"])
        analysis = await analyze(
            group_id,
            analysis_messages,
            users=users,
            features=features,
            persona=resolve_persona(session_id or group_id),
        )
        _resolve_names(analysis, analysis_messages)
        await _attach_avatars(bot, analysis)
        report = {
            "group_id": group_id,
            "group_name": group_name,
            "label": label,
            "report_date": report_date,
            "stats": stats,
            "analysis": analysis,
        }
        image = await render_report(report, theme=basic["theme"])
        identity = hashlib.sha256(group_id.encode("utf-8")).hexdigest()
        path = data_dir("group_analysis") / "images" / identity / f"{report_date}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{report_date}-{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(image)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        await GroupAnalysisReport.upsert(
            group_id=group_id,
            report_date=report_date,
            period_start=window.start,
            period_end=window.end,
            message_count=stats["total"],
            participant_count=stats["participants"],
            summary=json.dumps(analysis, ensure_ascii=False, default=str),
            image_path=str(path),
            created_at=utc_now_naive(),
        )
        logger.info(f"[group_analysis] 群 {group_id} 日报生成完成：{stats['total']} 条消息")
        return ReportResult(True, image=image)
    except (OSError, SQLAlchemyError):
        logger.opt(exception=True).warning(f"[group_analysis] 群 {group_id} 日报生成失败")
        return ReportResult(False, reason="failed")
    finally:
        _RUNNING.discard(group_id)
