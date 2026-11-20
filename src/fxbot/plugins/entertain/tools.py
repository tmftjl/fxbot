"""娱乐工具只消费本轮真实事件与原生发送能力。"""

from __future__ import annotations

from . import fortune
from .napcat import Likes
from .musicshare import (
    Music,
    MusicLoginRequired,
    MusicPlayUnavailable,
    _login_hint,
    _normalize_platform,
    _get_song_url_with_pool,
    _search_songs_with_pool,
)
from ...chat.tools import ToolError, ToolContext, ToolRuntime, tool
from ...core.types import Message, MessageSegment
from ...chat.session_hooks import session_target


async def authorized(ctx, rt, feature):
    event, bot = rt.require_event(), rt.require_bot()
    if rt.application is None or rt.authorizer is None or not rt.application.is_registered(bot):
        raise ToolError("工具没有当前在线连接")
    if ctx.user_id != event.user_id or not ctx.session_id or session_target(ctx.session_id) != event.target:
        raise ToolError("工具必须来自当前真实会话")
    if not await rt.authorizer.check(feature, event):
        raise ToolError("当前操作未获授权")
    return event, bot


@tool(
    name="get_fortune",
    title="今日运势",
    description="获取当前用户的今日运势。",
    permission="entertain:today",
    deferred=True,
    parameters={"type": "object", "properties": {"nickname": {"type": "string"}}},
)
async def get_fortune_tool(ctx: ToolContext, rt: ToolRuntime, nickname: str = "您") -> str:
    event, bot = await authorized(ctx, rt, "entertain:today")
    data, _ = fortune._get_or_create_today_fortune(event.user_id)
    image = await fortune.render_card_bytes(
        event.user_id, data, event.user_name or nickname, bot=bot
    )
    await authorized(ctx, rt, "entertain:today")
    await bot.send(event.target, Message([MessageSegment.image(image)]))
    return str(data["fortune"].get("fortuneSummary", "今日运势已发送"))


@tool(
    name="play_music",
    title="点歌",
    description="搜索并播放第一首歌曲。",
    permission="entertain:search",
    deferred=True,
    parameters={
        "type": "object",
        "properties": {"song_name": {"type": "string"}, "platform": {"type": "string", "enum": ["qq", "netease"]}},
        "required": ["song_name"],
    },
)
async def play_music_tool(ctx: ToolContext, rt: ToolRuntime, song_name: str, platform: str | None = None) -> str:
    event, bot = await authorized(ctx, rt, "entertain:search")
    if platform not in {None, "qq", "netease"} or not song_name.strip():
        raise ToolError("需要有效的平台和歌曲名")
    selected = _normalize_platform(platform)
    try:
        songs = await _search_songs_with_pool(Music.owner(event), selected, song_name)
        if not songs:
            raise ToolError("未找到歌曲")
        url = await _get_song_url_with_pool(Music.owner(event), selected, songs[0], bot=bot)
    except MusicLoginRequired as exc:
        raise ToolError(_login_hint(selected)) from exc
    except MusicPlayUnavailable as exc:
        raise ToolError("该歌曲暂时无法播放") from exc
    if not url:
        raise ToolError("未取得播放地址")
    await authorized(ctx, rt, "entertain:search")
    await bot.send(event.target, Message([MessageSegment.voice(url)]))
    return f"正在播放：{songs[0].song}"


@tool(
    name="like",
    title="QQ 点赞",
    description="为当前 QQ 用户点赞，每日一次，单次最多十个。",
    permission="entertain:like",
    deferred=True,
    parameters={"type": "object", "properties": {"times": {"type": "integer", "minimum": 1, "maximum": 10}}},
)
async def like_tool(ctx: ToolContext, rt: ToolRuntime, times: int = 10) -> dict:
    event, bot = await authorized(ctx, rt, "entertain:like")
    return {"message": await Likes().send(bot, event.user_id, times)}
