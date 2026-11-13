"""群聊日报原生命令。"""

from __future__ import annotations

import re
import time

from ...core import Message, Capability, HandlerContext, MessageSegment
from .config import bounded_days
from .report import REASON_TEXT, generate, is_running, manual_label, group_name_for
from ...utils.tz import today_str
from ...core.plugin import on_regex
from ...core.application import Application
from ...permission.authorization import Authorizer

COMMAND_NAME = "group_analysis_daily"
PATTERN = re.compile(r"^\s*[#＃/／]群聊分析(?:\s+(\d{1,2}))?\s*$")


@on_regex(PATTERN.pattern, name=COMMAND_NAME, display_name="群聊分析", level="all", scene="group")
async def analysis(context: HandlerContext) -> None:
    await handle_analysis(context, context.runtime.application, context.runtime.authorizer)


async def handle_analysis(context: HandlerContext, application: Application, authorizer: Authorizer) -> None:
    target, bot = context.event.target, context.bot
    if Capability.HISTORY not in bot.capabilities:
        await context.finish(REASON_TEXT["unsupported"])
    if is_running(target.key()):
        await context.finish(REASON_TEXT["busy"])
    match = PATTERN.fullmatch(context.arguments)
    days = bounded_days(int(match.group(1)) if match.group(1) else None)
    await context.send(f"正在统计{manual_label(days)}的群消息并生成日报，请稍候…")
    result = await generate(
        bot,
        target,
        days=days,
        end=int(time.time()),
        label=manual_label(days),
        report_date=today_str(),
        group_name=await group_name_for(bot, target),
        session_id=target.key(),
    )
    if not result.ok:
        await context.finish(REASON_TEXT[result.reason])
    if not application.is_registered(bot) or not await authorizer.check(
        f"group_analysis:{COMMAND_NAME}", context.event
    ):
        return
    await context.finish(Message([MessageSegment.image(result.image)]))
