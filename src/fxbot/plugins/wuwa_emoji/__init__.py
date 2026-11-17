"""鸣潮随机表情插件元信息。"""

from __future__ import annotations

import re

from loguru import logger

from . import store as store
from .store import lookup_character, refresh_characters
from ...core import Message, HandlerContext, MessageSegment
from .client import WuwaEmojiError, CharacterEmptyError, download_image, fetch_random_image
from ...core.plugin import PluginMetadata, on_regex, on_message

__plugin_meta__ = PluginMetadata(
    name="鸣潮表情",
    description="按角色名或别名发送随机鸣潮表情。",
    usage="ww随机 [角色名]；ww刷新（管理员）",
)

TRIGGER_PATTERN = re.compile(r"^/?ww随机\s*(\S*)$")

_TARGET_KEY = "_wuwa_emoji_target"


async def random_emoji_rule(context: HandlerContext) -> bool:
    """匹配随机表情命令并解析角色，未命中的角色让给其他路由。"""
    match = TRIGGER_PATTERN.match(context.event.message.plain_text().strip())
    if match is None:
        return False

    name = match.group(1)
    if not name:
        context.state[_TARGET_KEY] = ""
        return True

    # 正式名、slug、别名都来自数据而非硬编码；未命中时静默返回 False，让行给其他 matcher
    item = lookup_character(name)
    if item is None:
        return False

    context.state[_TARGET_KEY] = str(item["name"])
    return True


random_cmd = on_message(
    rule=random_emoji_rule,
    block=False,
    priority=5,
    name="random_emoji",
    display_name="随机鸣潮表情",
    level="member",
    scene="all",
)

refresh_cmd = on_regex(
    r"^/?ww(?:表情)?刷新$",
    name="refresh_characters",
    display_name="刷新鸣潮角色",
    priority=5,
    block=True,
    level="admin",
    scene="all",
)


@random_cmd.handle()
async def _handle_random(context: HandlerContext) -> None:
    """发送随机鸣潮表情。"""
    target = str(context.state.get(_TARGET_KEY) or "")
    try:
        image_url = await fetch_random_image(target or None)
        # QQ 官方平台由腾讯服务器代下媒体，只能由我们下好再发字节
        image_data = await download_image(image_url) if context.bot.platform == "qq" else image_url
    except CharacterEmptyError:
        await context.finish(f"{target}暂时没有可用的表情")
    except WuwaEmojiError:
        logger.opt(exception=True).warning(f"[wuwa_emoji] 随机表情请求失败：target={target or '(任意角色)'}")
        await context.finish()
    else:
        await context.finish(Message([MessageSegment.image(image_data)]))


@refresh_cmd.handle()
async def _handle_refresh(context: HandlerContext) -> None:
    """刷新角色列表缓存。"""
    try:
        characters = await refresh_characters()
    except WuwaEmojiError:
        logger.opt(exception=True).warning("[wuwa_emoji] 手动刷新角色列表失败")
        await context.finish("角色列表刷新失败，请稍后重试")
    else:
        await context.finish(f"角色列表已刷新，共 {len(characters)} 个角色")
