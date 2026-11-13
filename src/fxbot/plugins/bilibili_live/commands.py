"""B 站直播原生命令、参数解析和会话订阅操作。"""

from __future__ import annotations

import re

from .store import LiveStore
from ...core import Event, Message, SessionTarget, HandlerContext, MessageSegment
from .client import LiveRoomSnapshot, BilibiliLiveError, fetch_room, parse_room_id, fetch_room_by_uid
from ...core.plugin import on_regex

ROOM_ARGUMENT_PATTERN = r"(?:\d+|(?:https?://)?live\.bilibili\.com/\S+)"

store = LiveStore()


def _command_argument(event: Event, command: str) -> str:
    match = re.fullmatch(rf"(?:#|＃|/)?{re.escape(command)}\s*(.*)", event.message.plain_text().strip(), re.I)
    return match.group(1).strip() if match else ""


def _room_status(room: LiveRoomSnapshot) -> str:
    return "\n".join(
        [
            f"{room.name}（UID {room.uid}）",
            f"状态：{'直播中' if room.is_live else '未开播'}",
            f"标题：{room.title}",
            f"分区：{room.area}",
            f"直播间：{room.url}",
        ]
    )


async def _fetch_argument_room(event: Event, command: str) -> LiveRoomSnapshot:
    argument = _command_argument(event, command)
    if not argument:
        raise BilibiliLiveError("请在命令后填写直播间号或直播间链接")
    return await fetch_room(parse_room_id(argument))


class LiveCommands:
    """处理器只接收原生上下文，订阅目标来自真实入站会话。"""

    def __init__(self, store: LiveStore) -> None:
        self.store = store

    async def _save(self, context: HandlerContext, room: LiveRoomSnapshot) -> None:
        event = context.event
        actor = SessionTarget(event.platform, event.bot_id, "private", event.user_id).key()
        created = self.store.add_room(event.target, room, actor)
        location = "本群" if event.is_group else "当前私聊"
        text = (
            f"已为{location}订阅 {room.name} 的直播间 {room.room_id}"
            if created
            else f"{location}已经订阅了 {room.name} 的直播间 {room.room_id}"
        )
        await context.finish(text)

    async def subscribe(self, context: HandlerContext) -> None:
        try:
            room = await _fetch_argument_room(context.event, "B站直播订阅")
        except BilibiliLiveError as exc:
            await context.finish(str(exc))
        await self._save(context, room)

    async def subscribe_uid(self, context: HandlerContext) -> None:
        argument = _command_argument(context.event, "B站直播订阅UID").lstrip(":").strip()
        try:
            room = await fetch_room_by_uid(int(argument))
        except BilibiliLiveError as exc:
            await context.finish(str(exc))
        await self._save(context, room)

    async def unsubscribe(self, context: HandlerContext) -> None:
        try:
            room = await _fetch_argument_room(context.event, "B站直播取消")
        except BilibiliLiveError as exc:
            await context.finish(str(exc))
        location = "本群" if context.event.is_group else "当前私聊"
        if self.store.remove_room(context.event.target, room.room_id):
            await context.finish(f"已取消{location}对 {room.name} 直播间 {room.room_id} 的订阅")
        await context.finish(f"{location}没有订阅直播间 {room.room_id}")

    async def subscription_list(self, context: HandlerContext) -> None:
        subscription = self.store.subscription(context.event.target)
        if subscription is None or not subscription.rooms:
            await context.finish("当前会话没有 B 站直播订阅")
        lines = ["当前会话的 B 站直播订阅："]
        lines.extend(
            f"{index}. {room.name} - {room.room_id}" for index, room in enumerate(subscription.rooms.values(), start=1)
        )
        await context.finish("\n".join(lines))

    async def query(self, context: HandlerContext) -> None:
        try:
            room = await _fetch_argument_room(context.event, "B站直播查询")
        except BilibiliLiveError as exc:
            await context.finish(str(exc))
        segments = [MessageSegment.text(_room_status(room))]
        if room.cover:
            segments.append(MessageSegment.image(room.cover))
        await context.finish(Message(segments))


@on_regex(
    rf"^(?:#|＃|/)?B站直播订阅(?:\s*{ROOM_ARGUMENT_PATTERN})?$",
    name="bilibili_live_subscribe",
    display_name="订阅B站直播",
    level="admin",
    flags=re.I,
)
async def subscribe(context: HandlerContext) -> None:
    await LiveCommands(store).subscribe(context)


@on_regex(
    r"^(?:#|＃|/)?B站直播订阅UID\s*:?\s*\d+$",
    name="bilibili_live_subscribe_uid",
    display_name="按UID订阅B站直播",
    level="admin",
    flags=re.I,
)
async def subscribe_uid(context: HandlerContext) -> None:
    await LiveCommands(store).subscribe_uid(context)


@on_regex(
    rf"^(?:#|＃|/)?B站直播取消(?:\s*{ROOM_ARGUMENT_PATTERN})?$",
    name="bilibili_live_unsubscribe",
    display_name="取消B站直播",
    level="admin",
    flags=re.I,
)
async def unsubscribe(context: HandlerContext) -> None:
    await LiveCommands(store).unsubscribe(context)


@on_regex(
    r"^(?:#|＃|/)?B站直播列表$", name="bilibili_live_list", display_name="B站直播订阅列表", level="member", flags=re.I
)
async def subscription_list(context: HandlerContext) -> None:
    await LiveCommands(store).subscription_list(context)


@on_regex(
    rf"^(?:#|＃|/)?B站直播查询(?:\s*{ROOM_ARGUMENT_PATTERN})?$",
    name="bilibili_live_query",
    display_name="查询B站直播",
    level="member",
    flags=re.I,
)
async def query(context: HandlerContext) -> None:
    await LiveCommands(store).query(context)
