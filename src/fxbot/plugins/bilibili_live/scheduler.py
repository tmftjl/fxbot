"""B 站直播轮询、启动状态校准和逐订阅目标交付。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Awaitable

from loguru import logger

from .store import LiveStore
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
    """同一实例串行校准与轮询，不按平台动作失败猜测其他 Bot。"""

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
            subscriptions = self.store.subscriptions(room.room_id)
            if not subscriptions:
                continue
            previous = self.store.room_state(room.room_id)
            delivered = (
                previous.delivered
                if previous is not None and previous.is_live and room.is_live
                else frozenset(sub.target.key() for sub in subscriptions)
                if room.is_live
                else frozenset()
            )
            self.store.record_state(room.room_id, room.is_live, delivered)
        self._calibrated = True

    async def calibrate(self) -> None:
        """启动时建立基线，不补发停机期间开始的直播。"""
        async with self._lock:
            await self._calibrate()

    async def check(self, notifications: NotificationService, *, initial: bool = False) -> int:
        """每次应用首次运行只校准状态；通知服务由本次执行显式提供。"""
        return await self.check_and_push(notifications, initial=initial)

    def _subscribed(self, target: SessionTarget, room_id: int) -> bool:
        subscription = self.store.subscription(target)
        return subscription is not None and room_id in subscription.rooms

    def _pending(self, target: SessionTarget, room_id: int, generation: int) -> bool:
        status = self.store.room_state(room_id)
        return (
            self._subscribed(target, room_id)
            and status is not None
            and status.is_live
            and status.generation == generation
            and target.key() not in status.delivered
        )

    async def check_and_push(self, notifications: NotificationService | None = None, *, initial: bool = False) -> int:
        """离线目标保留待交付状态，其他目标成功不能替它确认交付。"""
        async with self._lock:
            if initial or not self._calibrated:
                await self._calibrate()
                return 0
            pushed = 0
            for room in await self._fetch_all():
                subscriptions = self.store.subscriptions(room.room_id)
                if not subscriptions:
                    continue
                previous = self.store.room_state(room.room_id)
                if not room.is_live:
                    self.store.record_state(room.room_id, False, frozenset())
                    continue
                if previous is None:
                    self.store.record_state(room.room_id, True, frozenset(sub.target.key() for sub in subscriptions))
                    continue
                if not previous.is_live:
                    self.store.record_state(room.room_id, True, frozenset())
                generation = self.store.room_state(room.room_id).generation
                segments = [MessageSegment.text(_push_text(room))]
                if room.cover:
                    segments.append(MessageSegment.image(room.cover))
                for sub in subscriptions:
                    status = self.store.room_state(room.room_id)
                    if status is None or not status.is_live or status.generation != generation:
                        break
                    if sub.target.key() in status.delivered:
                        continue
                    sender = notifications if notifications is not None else self.notifications
                    if sender is None:
                        raise ValueError("直播推送缺少通知服务")
                    sent = await sender.send(
                        "bilibili_live",
                        sub.target,
                        Message(segments),
                        valid=lambda target=sub.target, rid=room.room_id, generation=status.generation: self._pending(
                            target, rid, generation
                        ),
                    )
                    if sent:
                        pushed += 1
                        if self._subscribed(sub.target, room.room_id):
                            self.store.mark_delivered(room.room_id, sub.target, generation=status.generation)
            return pushed
