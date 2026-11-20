"""发病语录原生命令。"""

from __future__ import annotations

import httpx

from .config import cfg_api_urls
from ...core.types import Message, MessageSegment
from ...utils.http import get_shared_async_client
from ...core.plugin import on_regex
from ...core.router import HandlerContext

handle_matcher = on_regex(r"^(?:#|＃|/)?发病语录", name="get", display_name="发病语录", level="all")


@handle_matcher.handle()
async def handle(context: HandlerContext) -> None:
    url = cfg_api_urls()["sick_quote_api"].strip()
    if not url:
        await context.finish("未配置发病语录接口")
    try:
        response = await (await get_shared_async_client()).get(url)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("语录返回数据必须为对象")
    except (httpx.HTTPError, ValueError):
        await context.finish("获取发病语录失败，请稍后重试")
    text = str(data.get("message") or data.get("msg") or "")
    parts = [MessageSegment.text(text)]
    if context.event.target.kind == "group":
        parts.insert(0, MessageSegment.mention(context.event.user_id))
    await context.send(Message(parts))
