"""按完整会话身份持久化直播订阅与逐目标交付状态。"""

from __future__ import annotations

import os
import json
import time
from typing import Any
from pathlib import Path
from tempfile import NamedTemporaryFile
from dataclasses import fields, dataclass

from ...core import SessionTarget
from .client import LiveRoomSnapshot
from ...utils.paths import data_dir


@dataclass(frozen=True, slots=True)
class Subscription:
    """订阅目标不包含可猜测的群号或机器人别名。"""

    target: SessionTarget
    operator_id: str
    rooms: dict[int, LiveRoomSnapshot]


@dataclass(frozen=True, slots=True)
class RoomState:
    """已交付目标按直播间当前开播周期独立记录。"""

    is_live: bool
    delivered: frozenset[str]
    checked_at: int
    generation: int


@dataclass(slots=True)
class _State:
    subscriptions: dict[str, Subscription]
    room_states: dict[int, RoomState]


def _object(value: Any, names: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or (names is not None and set(value) != names):
        raise ValueError("直播订阅状态结构不正确")
    return value


def _positive(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("直播间和主播 ID 必须为正整数")
    return value


def _room_key(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or str(int(value)) != value:
        raise ValueError("直播间记录键不是规范十进制 ID")
    return _positive(int(value))


def _target(key: str) -> SessionTarget:
    if not isinstance(key, str):
        raise ValueError("直播订阅目标必须为规范会话键")
    target = SessionTarget.from_key(key)
    if target.kind not in {"group", "private"}:
        raise ValueError("直播订阅仅支持群聊和私聊")
    return target


def _decode(raw: Any) -> _State:
    data = _object(raw)
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("直播订阅状态版本不受支持，必须先明确平台和 Bot 后迁移")
    _object(data, {"version", "subscriptions", "room_states"})
    subscriptions = {}
    for key, value in _object(data["subscriptions"]).items():
        target = _target(key)
        entry = _object(value, {"operator_id", "rooms"})
        actor = _target(entry["operator_id"])
        if actor.kind != "private" or (actor.platform, actor.bot_id) != (target.platform, target.bot_id):
            raise ValueError("订阅操作者与会话不属于同一连接")
        rooms = {}
        for room_key, record in _object(entry["rooms"]).items():
            room_id = _room_key(room_key)
            record = _object(record, {field.name for field in fields(LiveRoomSnapshot)})
            _positive(record["room_id"])
            _positive(record["uid"])
            if record["room_id"] != room_id or type(record["live_status"]) is not int or record["live_status"] < 0:
                raise ValueError("直播间记录身份或状态无效")
            if any(
                not isinstance(record[field.name], str)
                for field in fields(LiveRoomSnapshot)
                if field.name not in {"room_id", "uid", "live_status"}
            ):
                raise ValueError("直播间文本字段必须是字符串")
            rooms[room_id] = LiveRoomSnapshot(**record)
        subscriptions[key] = Subscription(target, entry["operator_id"], rooms)
    room_states = {}
    for room_key, value in _object(data["room_states"]).items():
        entry = _object(value, {"is_live", "delivered", "checked_at", "generation"})
        delivered = entry["delivered"]
        if type(entry["is_live"]) is not bool or not isinstance(delivered, list):
            raise ValueError("直播状态和交付目标无效")
        if type(entry["checked_at"]) is not int or entry["checked_at"] < 0:
            raise ValueError("直播检查时间必须为非负整数")
        if type(entry["generation"]) is not int or entry["generation"] < 0:
            raise ValueError("直播开播代次必须为非负整数")
        for key in delivered:
            _target(key)
        if len(set(delivered)) != len(delivered):
            raise ValueError("直播交付目标不能重复")
        room_states[_room_key(room_key)] = RoomState(
            entry["is_live"], frozenset(delivered), entry["checked_at"], entry["generation"]
        )
    return _State(subscriptions, room_states)


class LiveStore:
    """同步读改写不跨 await，文件损坏不能被空状态覆盖。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else data_dir("bilibili_live") / "state.json"

    def _read(self) -> _State:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _State({}, {})
        return _decode(json.loads(text))

    def validate(self) -> None:
        """启动阶段验证既有状态，不改写旧格式或坏文件。"""
        self._read()

    def _write(self, state: _State) -> None:
        raw = {
            "version": 1,
            "subscriptions": {
                key: {
                    "operator_id": sub.operator_id,
                    "rooms": {str(rid): room.to_record() for rid, room in sub.rooms.items()},
                }
                for key, sub in state.subscriptions.items()
            },
            "room_states": {
                str(rid): {
                    "is_live": status.is_live,
                    "delivered": sorted(status.delivered),
                    "checked_at": status.checked_at,
                    "generation": status.generation,
                }
                for rid, status in state.room_states.items()
            },
        }
        _decode(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix="state-", suffix=".tmp", delete=False
            ) as output:
                temporary = Path(output.name)
                json.dump(raw, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def subscriptions(self, room_id: int | None = None) -> tuple[Subscription, ...]:
        return tuple(sub for sub in self._read().subscriptions.values() if room_id is None or room_id in sub.rooms)

    def subscription(self, target: SessionTarget) -> Subscription | None:
        return self._read().subscriptions.get(target.key())

    def add_room(self, target: SessionTarget, room: LiveRoomSnapshot, operator_id: str) -> bool:
        state = self._read()
        key = target.key()
        previous = state.subscriptions.get(key)
        rooms = dict(previous.rooms) if previous else {}
        created = room.room_id not in rooms
        rooms[room.room_id] = room
        state.subscriptions[key] = Subscription(target, operator_id, rooms)
        status = state.room_states.get(room.room_id)
        delivered = status.delivered if status is not None and status.is_live and room.is_live else frozenset()
        if room.is_live:
            delivered = delivered | {key}
        generation = (status.generation if status is not None else 0) + int(
            room.is_live and (status is None or not status.is_live)
        )
        state.room_states[room.room_id] = RoomState(room.is_live, frozenset(delivered), int(time.time()), generation)
        self._write(state)
        return created

    def remove_room(self, target: SessionTarget, room_id: int) -> bool:
        state = self._read()
        key = target.key()
        previous = state.subscriptions.get(key)
        if previous is None or room_id not in previous.rooms:
            return False
        rooms = dict(previous.rooms)
        rooms.pop(room_id)
        if rooms:
            state.subscriptions[key] = Subscription(target, previous.operator_id, rooms)
        else:
            state.subscriptions.pop(key)
        if not any(room_id in sub.rooms for sub in state.subscriptions.values()):
            state.room_states.pop(room_id, None)
        elif (status := state.room_states.get(room_id)) is not None:
            state.room_states[room_id] = RoomState(
                status.is_live, status.delivered - {key}, status.checked_at, status.generation
            )
        self._write(state)
        return True

    def rooms(self) -> tuple[LiveRoomSnapshot, ...]:
        result = {}
        for sub in self.subscriptions():
            for room_id, room in sub.rooms.items():
                result.setdefault(room_id, room)
        return tuple(result.values())

    def room_state(self, room_id: int) -> RoomState | None:
        return self._read().room_states.get(room_id)

    def record_state(self, room_id: int, is_live: bool, delivered: frozenset[str]) -> None:
        state = self._read()
        previous = state.room_states.get(room_id)
        if previous is not None and (previous.is_live, previous.delivered) == (is_live, delivered):
            return
        generation = (previous.generation if previous is not None else 0) + int(
            is_live and (previous is None or not previous.is_live)
        )
        state.room_states[room_id] = RoomState(is_live, delivered, int(time.time()), generation)
        self._write(state)

    def mark_delivered(self, room_id: int, target: SessionTarget, *, generation: int) -> bool:
        previous = self.room_state(room_id)
        if previous is None or not previous.is_live or previous.generation != generation:
            return False
        self.record_state(room_id, True, previous.delivered | {target.key()})
        return True
