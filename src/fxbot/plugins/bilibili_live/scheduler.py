"""B 站直播轮询、启动状态校准和开播推送。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Awaitable

from loguru import logger

from .store import LiveStore, Subscription
from ...core import Message, SessionTarget, MessageSegment
from .client import LiveRoomSnapshot, BilibiliLiveError, fetch_room
from ...runtime.notifications import NotificationService

CHECK_INTERVAL_SECONDS = 60
MAX_CONCURRENCY = 4
RoomFetcher = Callable[..., Awaitable[LiveRoomSnapshot]]


def _push_text(room: LiveRoomSnapshot) -> str:
    lines = [f"[B站直播] {room.name} 开播了", f"标题：{room.title}", f"分区：{room.area}"]
    if room.live_time and room.live_time != "0000-00-00 00:00:00":
        lines.append(f"开播时间：{room.live_time}")
    lines.append(room.url)
    return "\n".join(lines)


class LivePoller:
    """按群号/用户 ID 订阅，推送时依次尝试在线 Bot。"""

    def __init__(
        self, store: LiveStore, notifications: NotificationService | None = None, fetcher: RoomFetcher = fetch_room
    ) -> None:
        self.store = store
        self.notifications = notifications
        self.fetcher = fetcher
        self._lock = asyncio.Lock()
        self._calibrated = False

    async def _fetch_all(self) -> list[LiveRoomSnapshot]:
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def fetch(record: LiveRoomSnapshot) -> LiveRoomSnapshot | None:
            async with semaphore:
                try:
                    result = await self.fetcher(record.room_id, known_name=record.name)
                except BilibiliLiveError as exc:
                    logger.warning("[bilibili_live] 直播间 {} 查询失败：{}", record.room_id, exc)
                    return None
                if result.room_id != record.room_id:
                    raise ValueError("直播轮询返回了不同房间")
                return result

        results = await asyncio.gather(*(fetch(record) for record in self.store.rooms()), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return [room for room in results if room is not None]

    async def _calibrate(self) -> None:
        for room in await self._fetch_all():
            if not self.store.subscriptions(room.room_id):
                continue
            self.store.record_state(room.room_id, room.is_live, room.is_live)
        self._calibrated = True

    async def calibrate(self) -> None:
        """启动时建立基线，不补发停机期间开始的直播。"""
        async with self._lock:
            await self._calibrate()

    async def check(self, notifications: NotificationService, *, initial: bool = False) -> int:
        """每次应用首次运行只校准状态；通知服务由本次执行显式提供。"""
        return await self.check_and_push(notifications, initial=initial)

    def _subscribed(self, sub: Subscription, room_id: int) -> bool:
        current = self.store.subscription(sub.kind, sub.target_id)
        return current is not None and room_id in current.rooms

    def _pending(self, sub: Subscription, room_id: int) -> bool:
        status = self.store.room_state(room_id)
        return self._subscribed(sub, room_id) and status is not None and status.is_live and not status.delivered

    async def check_and_push(self, notifications: NotificationService | None = None, *, initial: bool = False) -> int:
        """同一开播周期只推送一次；没有可用 Bot 时保留待推送状态。"""
        async with self._lock:
            if initial or not self._calibrated:
                await self._calibrate()
                return 0
            sender = notifications if notifications is not None else self.notifications
            if sender is None:
                raise ValueError("直播推送缺少通知服务")
            pushed = 0
            for room in await self._fetch_all():
                subscriptions = self.store.subscriptions(room.room_id)
                if not subscriptions:
                    continue
                previous = self.store.room_state(room.room_id)
                if not room.is_live:
                    self.store.record_state(room.room_id, False, False)
                    continue
                if previous is None or not previous.is_live:
                    self.store.record_state(room.room_id, True, False)
                current = self.store.room_state(room.room_id)
                if current is not None and current.delivered:
                    continue
                segments = [MessageSegment.text(_push_text(room))]
                if room.cover:
                    segments.append(MessageSegment.image(room.cover))
                delivered = False
                for sub in subscriptions:
                    for bot in sender.application.bots:
                        target = SessionTarget(bot.platform, bot.bot_id, sub.kind, sub.target_id)
                        sent = await sender.send(
                            "bilibili_live",
                            target,
                            Message(segments),
                            valid=lambda sub=sub, rid=room.room_id: self._pending(sub, rid),
                        )
                        if sent:
                            delivered = True
                            pushed += 1
                            break
                if delivered:
                    self.store.mark_delivered(room.room_id)
            return pushed
