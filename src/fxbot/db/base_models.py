"""数据库基础层。"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, TypeVar, Callable, Awaitable, ParamSpec, Concatenate
from functools import wraps

from loguru import logger
from sqlmodel import Field, SQLModel, select
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..utils.paths import database_path

T = TypeVar("T", bound="BaseIDModel")
P = ParamSpec("P")
R = TypeVar("R")

DB_PATH = database_path()
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine: AsyncEngine | None = None
async_maker: async_sessionmaker[AsyncSession] | None = None
sqlite_semaphore: asyncio.Semaphore | None = None

_db_init_lock = asyncio.Lock()
_db_initialized = False


async def init_database() -> None:
    """初始化连接池及已登记模型，失败时释放部分资源并直接抛出。"""
    global _db_initialized, engine, async_maker, sqlite_semaphore
    async with _db_init_lock:
        if _db_initialized:
            return
        if engine is not None:
            raise RuntimeError("数据库连接资源尚未释放")
        candidate = create_async_engine(
            DB_URL,
            echo=False,
            pool_recycle=1800,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(candidate.sync_engine, "connect")
        def configure_sqlite(connection: sqlite3.Connection, record: Any) -> None:
            """为每条连接设置确定的 SQLite 会话参数。"""
            cursor = connection.cursor()
            try:
                for statement in (
                    "PRAGMA journal_mode=WAL",
                    "PRAGMA synchronous=NORMAL",
                    "PRAGMA busy_timeout=5000",
                    "PRAGMA cache_size=20000",
                    "PRAGMA temp_store=MEMORY",
                    "PRAGMA mmap_size=134217728",
                    "PRAGMA optimize",
                ):
                    cursor.execute(statement)
            finally:
                cursor.close()

        ready = False
        try:
            async with candidate.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            engine = candidate
            async_maker = async_sessionmaker(candidate, expire_on_commit=False, class_=AsyncSession)
            sqlite_semaphore = asyncio.Semaphore(20)
            _db_initialized = True
            ready = True
            logger.info("[DB] SQLite initialized")
        finally:
            if not ready:
                await candidate.dispose()


def is_initialized() -> bool:
    """数据库是否已初始化。"""
    return _db_initialized


async def close_database() -> None:
    """释放连接池并清空可用状态，失败不伪装成关闭成功。"""
    global engine, async_maker, sqlite_semaphore, _db_initialized
    async with _db_init_lock:
        _db_initialized = False
        if engine is not None:
            await engine.dispose()
        engine = None
        async_maker = None
        sqlite_semaphore = None


def with_session(
    func: Callable[Concatenate[Any, AsyncSession, P], Awaitable[R]],
) -> Callable[Concatenate[Any, P], Awaitable[R]]:
    """为方法注入 AsyncSession。"""

    @wraps(func)
    async def wrapper(self, *args: P.args, **kwargs: P.kwargs):
        if not _db_initialized:
            raise RuntimeError("数据库尚未初始化，请先调用 init_database()")

        session = kwargs.pop("session", None)
        if session is not None:
            return await func(self, session, *args, **kwargs)

        async with async_maker() as new_session:  # type: ignore[operator]
            result = await func(self, new_session, *args, **kwargs)
            await new_session.commit()
            return result

    return wrapper


class BaseIDModel(SQLModel):
    """带自增主键的基础模型。"""

    id: int | None = Field(default=None, primary_key=True, title="id")

    @classmethod
    @with_session
    async def get_by_ids(
        cls: type[T],
        session: AsyncSession,
        ids: list[int],
    ) -> list[T]:
        """按主键批量查询。"""
        if not ids:
            return []
        stmt = select(cls).where(cls.id.in_(ids))  # type: ignore[attr-defined]
        result = await session.execute(stmt)
        return result.scalars().all()

    @classmethod
    @with_session
    async def select_rows(
        cls: type[T],
        session: AsyncSession,
        **conditions: Any,
    ) -> list[T]:
        """按等值条件查询多行。"""
        stmt = select(cls)
        if conditions:
            stmt = stmt.where(*[getattr(cls, k) == v for k, v in conditions.items()])
        result = await session.execute(stmt)
        return result.scalars().all()

    @classmethod
    @with_session
    async def _batch_insert_or_update(
        cls: type[T],
        session: AsyncSession,
        datas: list[dict[str, Any]],
        update_keys: list[str],
        index_elements: list[str],
    ) -> None:
        """批量插入或更新，仅实现 SQLite 版本。"""
        if not datas:
            return

        from sqlalchemy.dialects.sqlite import insert

        stmt = insert(cls).values(datas)
        update_stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={k: stmt.excluded[k] for k in update_keys},
        )
        await session.execute(update_stmt)
