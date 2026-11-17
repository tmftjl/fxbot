"""B 站直播订阅与开播状态持久化。"""

from __future__ import annotations

import os
import json
import time
from typing import Any
from pathlib import Path
from tempfile import NamedTemporaryFile
from dataclasses import fields, dataclass

from .client import LiveRoomSnapshot
from ...utils.paths import data_dir


@dataclass(frozen=True, slots=True)
class Subscription:
    """直播订阅只记录群号或用户 ID。"""

    kind: str
    target_id: str
    operator_id: str
    rooms: dict[int, LiveRoomSnapshot]

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target_id}"


@dataclass(frozen=True, slots=True)
class RoomState:
    """直播间当前开播周期与是否已经推送。"""

    is_live: bool
    delivered: bool
    checked_at: int


@dataclass(slots=True)
class _State:
    subscriptions: list[Subscription]
    room_states: dict[int, RoomState]


def _object(value: Any, names: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or (names is not None and not names <= set(value)):
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


def _bare_id(value: Any, label: str) -> str:
    if type(value) is int and value > 0:
        return str(value)
    if not isinstance(value, str) or not value or value != value.strip() or ":" in value:
        raise ValueError(f"{label}必须为群号或用户 ID")
    return value


def _decode(raw: Any) -> _State:
    data = _object(raw, {"subscriptions", "room_states"})
    entries = data["subscriptions"]
    if not isinstance(entries, list):
        raise ValueError("直播订阅必须为数组")
    subscriptions = []
    seen = set()
    for value in entries:
        entry = _object(value)
        kind = entry.get("type")
        if kind not in {"group", "private"}:
            raise ValueError("直播订阅类型必须为 group 或 private")
        key_field = "group_key" if kind == "group" else "user_key"
        target_id = _bare_id(entry.get(key_field), "订阅目标")
        operator_id = _bare_id(entry.get("operator_id"), "订阅操作者")
        rooms = {}
        for room_key, record in _object(entry.get("rooms")).items():
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
        key = f"{kind}:{target_id}"
        if key in seen:
            raise ValueError("直播订阅目标不能重复")
        seen.add(key)
        subscriptions.append(Subscription(kind, target_id, operator_id, rooms))
    room_states = {}
    for room_key, value in _object(data["room_states"]).items():
        entry = _object(value, {"is_live", "checked_at"})
        if type(entry.get("is_live")) is not bool or type(entry.get("delivered", False)) is not bool:
            raise ValueError("直播状态和交付标记无效")
        if type(entry.get("checked_at")) is not int or entry["checked_at"] < 0:
            raise ValueError("直播检查时间必须为非负整数")
        room_states[_room_key(room_key)] = RoomState(
            entry["is_live"], entry.get("delivered", False), entry["checked_at"]
        )
    return _State(subscriptions, room_states)


class LiveStore:
    """同步读改写不跨 await，坏文件不能被空状态覆盖。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else data_dir("bilibili_live") / "state.json"
        # 旧文件仅保存整轮交付标记；逐目标进度限于本次运行，重启仍由轮询器校准。
        self._delivered: dict[int, set[str]] = {}

    def _read(self) -> _State:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _State([], {})
        return _decode(json.loads(text))

    def validate(self) -> None:
        self._read()

    def _write(self, state: _State) -> None:
        raw = {
            "subscriptions": [
                {
                    "type": sub.kind,
                    "group_key" if sub.kind == "group" else "user_key": sub.target_id,
                    "operator_id": sub.operator_id,
                    "rooms": {str(rid): room.to_record() for rid, room in sub.rooms.items()},
                }
                for sub in state.subscriptions
            ],
            "room_states": {
                str(rid): {
                    "is_live": status.is_live,
                    "delivered": status.delivered,
                    "checked_at": status.checked_at,
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
        return tuple(sub for sub in self._read().subscriptions if room_id is None or room_id in sub.rooms)

    def subscription(self, kind: str, target_id: str) -> Subscription | None:
        key = f"{kind}:{target_id}"
        return next((sub for sub in self._read().subscriptions if sub.key == key), None)

    def add_room(self, kind: str, target_id: str, room: LiveRoomSnapshot, operator_id: str) -> bool:
        _bare_id(target_id, "订阅目标")
        _bare_id(operator_id, "订阅操作者")
        if kind not in {"group", "private"}:
            raise ValueError("直播订阅类型必须为 group 或 private")
        state = self._read()
        key = f"{kind}:{target_id}"
        previous = next((sub for sub in state.subscriptions if sub.key == key), None)
        rooms = dict(previous.rooms) if previous is not None else {}
        created = room.room_id not in rooms
        rooms[room.room_id] = room
        if previous is not None:
            state.subscriptions = [sub for sub in state.subscriptions if sub.key != key]
        state.subscriptions.append(Subscription(kind, target_id, operator_id, rooms))
        if room.is_live and room.room_id not in state.room_states:
            # 订阅时正在直播，不补发本轮开播通知。
            state.room_states[room.room_id] = RoomState(True, True, int(time.time()))
        elif room.room_id not in state.room_states:
            state.room_states[room.room_id] = RoomState(False, False, int(time.time()))
        self._write(state)
        if room.is_live and created:
            self._delivered.setdefault(room.room_id, set()).add(key)
        return created

    def remove_room(self, kind: str, target_id: str, room_id: int) -> bool:
        key = f"{kind}:{target_id}"
        state = self._read()
        previous = next((sub for sub in state.subscriptions if sub.key == key), None)
        if previous is None or room_id not in previous.rooms:
            return False
        rooms = dict(previous.rooms)
        rooms.pop(room_id)
        state.subscriptions = [sub for sub in state.subscriptions if sub.key != key]
        if rooms:
            state.subscriptions.append(Subscription(kind, target_id, previous.operator_id, rooms))
        if not any(room_id in sub.rooms for sub in state.subscriptions):
            state.room_states.pop(room_id, None)
        self._write(state)
        self._delivered.get(room_id, set()).discard(key)
        return True

    def rooms(self) -> tuple[LiveRoomSnapshot, ...]:
        result = {}
        for sub in self.subscriptions():
            for room_id, room in sub.rooms.items():
                result.setdefault(room_id, room)
        return tuple(result.values())

    def room_state(self, room_id: int) -> RoomState | None:
        return self._read().room_states.get(room_id)

    def record_state(self, room_id: int, is_live: bool, delivered: bool) -> None:
        state = self._read()
        previous = state.room_states.get(room_id)
        if previous is None or (previous.is_live, previous.delivered) != (is_live, delivered):
            state.room_states[room_id] = RoomState(is_live, delivered, int(time.time()))
            self._write(state)
        if not is_live or delivered:
            self._delivered.pop(room_id, None)

    def target_pending(self, room_id: int, key: str) -> bool:
        return key not in self._delivered.get(room_id, set())

    def mark_target_delivered(self, room_id: int, key: str) -> None:
        self._delivered.setdefault(room_id, set()).add(key)

    def mark_delivered(self, room_id: int) -> bool:
        previous = self.room_state(room_id)
        if previous is None or not previous.is_live or previous.delivered:
            return False
        self.record_state(room_id, True, True)
        return True
