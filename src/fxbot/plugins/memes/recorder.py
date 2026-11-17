"""表情调用记录读写；统计沿用旧会话表关联。"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Optional
from weakref import WeakKeyDictionary
from datetime import datetime, timezone
from dataclasses import dataclass

from sqlmodel import select
from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import with_session
from ...core import Event
from .models import MemeGenerationRecord
from .session_models import SessionBot, SessionUser, SessionScene, SessionIdentity


@dataclass(slots=True)
class MemeRecord:
    time: datetime
    meme_key: str


class SessionIdType(Enum):
    GLOBAL = 0
    USER = 1
    GROUP = 2
    GROUP_USER = 3


_SCOPE = "Unknown"
_ROLES = {
    "owner": {"id": "OWNER", "level": 100, "name": "owner"},
    "admin": {"id": "ADMINISTRATOR", "level": 10, "name": "admin"},
    "member": {"id": "MEMBER", "level": 1, "name": "member"},
}


def adapter_name(platform: str) -> str:
    """把新运行时的平台名映射为会话表的适配器名。"""
    if platform == "onebot11":
        return "OneBot V11"
    if platform == "qq":
        return "QQ"
    if platform.startswith("onebot12."):
        return "OneBot V12"
    return platform


def _scene_type(event: Event) -> int:
    return 1 if event.is_group else 0


def _scene_id(event: Event) -> str:
    return event.target.target_id if event.is_group else event.user_id


def _user_data(event: Event) -> dict[str, Any]:
    name = event.user_name or event.user_id
    return {
        "id": event.user_id,
        "name": name,
        "nick": name,
        "avatar": None,
        "gender": "unknown",
    }


def _member_data(event: Event, user: dict[str, Any]) -> dict[str, Any] | None:
    if not event.is_group:
        return None
    role = event.metadata.get("sender_role")
    role_data = _ROLES.get(role if isinstance(role, str) else "member", _ROLES["member"])
    return {
        "user": dict(user),
        "nick": user["nick"],
        "mute": None,
        "joined_at": None,
        "roles": [dict(role_data)],
    }


class _MemeRecordStore:
    """表情调用记录存储。"""

    async def _bot_persist_id(self, db_session: AsyncSession, event: Event) -> int:
        adapter = adapter_name(event.platform)
        statement = select(SessionBot).where(
            SessionBot.self_id == event.bot_id,
            SessionBot.adapter == adapter,
        )
        row = (await db_session.execute(statement)).scalar_one_or_none()
        if row is not None:
            row.scope = _SCOPE
            await db_session.flush()
            assert row.id is not None
            return row.id
        row = SessionBot(self_id=event.bot_id, adapter=adapter, scope=_SCOPE)
        db_session.add(row)
        await db_session.flush()
        assert row.id is not None
        return row.id

    async def _scene_persist_id(self, db_session: AsyncSession, event: Event, bot_persist_id: int) -> int:
        scene_type = _scene_type(event)
        scene_id = _scene_id(event)
        statement = select(SessionScene).where(
            SessionScene.bot_persist_id == bot_persist_id,
            SessionScene.scene_id == scene_id,
            SessionScene.scene_type == scene_type,
        )
        row = (await db_session.execute(statement)).scalar_one_or_none()
        if row is not None:
            data = dict(row.scene_data)
            data.update({"id": scene_id, "type": scene_type, "parent": None})
            row.scene_data = data
            await db_session.flush()
            assert row.id is not None
            return row.id
        data = {
            "id": scene_id,
            "type": scene_type,
            "name": event.user_name if not event.is_group else None,
            "avatar": None,
            "parent": None,
        }
        row = SessionScene(
            bot_persist_id=bot_persist_id,
            parent_scene_persist_id=None,
            scene_id=scene_id,
            scene_type=scene_type,
            scene_data=data,
        )
        db_session.add(row)
        await db_session.flush()
        assert row.id is not None
        return row.id

    async def _user_persist_id(
        self,
        db_session: AsyncSession,
        event: Event,
        bot_persist_id: int,
    ) -> tuple[int, dict[str, Any]]:
        data = _user_data(event)
        statement = select(SessionUser).where(
            SessionUser.bot_persist_id == bot_persist_id,
            SessionUser.user_id == event.user_id,
        )
        row = (await db_session.execute(statement)).scalar_one_or_none()
        if row is not None:
            merged = dict(row.user_data)
            merged.update(data)
            row.user_data = merged
            await db_session.flush()
            assert row.id is not None
            return row.id, merged
        row = SessionUser(bot_persist_id=bot_persist_id, user_id=event.user_id, user_data=data)
        db_session.add(row)
        await db_session.flush()
        assert row.id is not None
        return row.id, data

    async def _session_persist_id(self, db_session: AsyncSession, event: Event) -> int:
        bot_persist_id = await self._bot_persist_id(db_session, event)
        scene_persist_id = await self._scene_persist_id(db_session, event, bot_persist_id)
        user_persist_id, user_data = await self._user_persist_id(db_session, event, bot_persist_id)
        member_data = _member_data(event, user_data)
        statement = select(SessionIdentity).where(
            SessionIdentity.bot_persist_id == bot_persist_id,
            SessionIdentity.scene_persist_id == scene_persist_id,
            SessionIdentity.user_persist_id == user_persist_id,
        )
        row = (await db_session.execute(statement)).scalar_one_or_none()
        if row is not None:
            row.member_data = member_data
            await db_session.flush()
            assert row.id is not None
            return row.id
        row = SessionIdentity(
            bot_persist_id=bot_persist_id,
            scene_persist_id=scene_persist_id,
            user_persist_id=user_persist_id,
            member_data=member_data,
        )
        db_session.add(row)
        await db_session.flush()
        assert row.id is not None
        return row.id

    @with_session
    async def add_record(self, db_session: AsyncSession, event: Event, meme_key: str) -> None:
        session_persist_id = await self._session_persist_id(db_session, event)
        db_session.add(
            MemeGenerationRecord(
                session_persist_id=session_persist_id,
                time=datetime.now(timezone.utc),
                meme_key=meme_key,
            )
        )

    @with_session
    async def list_records(self, db_session: AsyncSession, statement: Any) -> list[MemeRecord]:
        results = (await db_session.execute(statement)).all()
        return [MemeRecord(result[0], result[1]) for result in results]

    @with_session
    async def list_scalars(self, db_session: AsyncSession, statement: Any) -> list[Any]:
        return list((await db_session.execute(statement)).scalars().all())


_store = _MemeRecordStore()
_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


def _write_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _locks[loop] = lock
    return lock


async def record_meme_generation(event: Event, meme_key: str) -> None:
    """写入一次成功调用；同一循环内串行化旧会话表的查找或创建。"""
    async with _write_lock():
        await _store.add_record(event, meme_key)


def filter_statement(
    event: Event,
    id_type: SessionIdType,
    *,
    meme_key: Optional[str] = None,
    time_start: Optional[datetime] = None,
    time_stop: Optional[datetime] = None,
) -> list[ColumnElement[bool]]:
    filter_scene = id_type in {SessionIdType.GROUP, SessionIdType.GROUP_USER}
    filter_user = id_type in {SessionIdType.USER, SessionIdType.GROUP_USER}
    whereclause: list[ColumnElement[bool]] = [
        SessionBot.self_id == event.bot_id,
        SessionBot.adapter == adapter_name(event.platform),
    ]
    if filter_scene:
        whereclause.append(SessionScene.scene_id == _scene_id(event))
        whereclause.append(SessionScene.scene_type == _scene_type(event))
    if filter_user:
        whereclause.append(SessionUser.user_id == event.user_id)
    if meme_key:
        whereclause.append(MemeGenerationRecord.meme_key == meme_key)
    if time_start:
        whereclause.append(MemeGenerationRecord.time >= time_start)
    if time_stop:
        whereclause.append(MemeGenerationRecord.time <= time_stop)
    return whereclause


def _records_statement(columns: tuple[Any, ...], event: Event, id_type: SessionIdType, **kwargs: Any):
    whereclause = filter_statement(event, id_type, **kwargs)
    return (
        select(*columns)
        .where(*whereclause)
        .join(SessionIdentity, SessionIdentity.id == MemeGenerationRecord.session_persist_id)
        .join(SessionBot, SessionBot.id == SessionIdentity.bot_persist_id)
        .join(SessionScene, SessionScene.id == SessionIdentity.scene_persist_id)
        .join(SessionUser, SessionUser.id == SessionIdentity.user_persist_id)
    )


async def get_meme_generation_records(
    event: Event,
    id_type: SessionIdType,
    *,
    meme_key: Optional[str] = None,
    time_start: Optional[datetime] = None,
    time_stop: Optional[datetime] = None,
) -> list[MemeRecord]:
    statement = _records_statement(
        (MemeGenerationRecord.time, MemeGenerationRecord.meme_key),
        event,
        id_type,
        meme_key=meme_key,
        time_start=time_start,
        time_stop=time_stop,
    )
    return await _store.list_records(statement)


async def get_meme_generation_times(
    event: Event,
    id_type: SessionIdType,
    *,
    meme_key: Optional[str] = None,
    time_start: Optional[datetime] = None,
    time_stop: Optional[datetime] = None,
) -> list[datetime]:
    statement = _records_statement(
        (MemeGenerationRecord.time,),
        event,
        id_type,
        meme_key=meme_key,
        time_start=time_start,
        time_stop=time_stop,
    )
    return await _store.list_scalars(statement)


async def get_meme_generation_keys(
    event: Event,
    id_type: SessionIdType,
    *,
    time_start: Optional[datetime] = None,
    time_stop: Optional[datetime] = None,
) -> list[str]:
    statement = _records_statement(
        (MemeGenerationRecord.meme_key,),
        event,
        id_type,
        time_start=time_start,
        time_stop=time_stop,
    )
    return await _store.list_scalars(statement)
