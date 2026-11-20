"""Doro 结局原生命令。"""

from __future__ import annotations

import httpx

from .config import cfg_api_urls
from ...core.types import Message, MessageSegment
from ...utils.http import get_shared_async_client
from ...core.plugin import on_regex
from ...core.router import HandlerContext

handle_matcher = on_regex(
    r"^[#＃]?(?:抽取|随机)?(?:今日)?doro结局", name="draw", display_name="doro结局", level="all"
)


@handle_matcher.handle()
async def handle(context: HandlerContext) -> None:
    url = cfg_api_urls()["doro_api"].strip()
    if not url:
        await context.finish("未配置 doro 接口")
    try:
        response = await (await get_shared_async_client()).get(url)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("doro 返回数据必须为对象")
    except (httpx.HTTPError, ValueError):
        await context.finish("获取 doro 结局失败，请稍后重试")
    parts = [MessageSegment.text(f"今日doro结局：\n\n{data.get('title', '')}\n\n{data.get('description', '')}")]
    if image := data.get("image"):
        if not isinstance(image, str):
            raise ValueError("doro 图片地址必须为字符串")
        parts.append(MessageSegment.image(image))
    await context.send(Message(parts))
