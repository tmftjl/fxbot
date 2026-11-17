"""会话身份表的名称迁移：把历史表名与约束名改写为新名称。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import create_engine

from .session_models import SessionBot, SessionUser, SessionScene, SessionIdentity

# 历史表按后缀识别，不依赖具体前缀；新表名见 TABLE_SUFFIXES 的值。
TABLE_SUFFIXES = {
    "uninfo_botmodel": "session_bot",
    "uninfo_scenemodel": "session_scene",
    "uninfo_usermodel": "session_user",
    "uninfo_sessionmodel": "session_identity",
}

_MODELS = (SessionBot, SessionScene, SessionUser, SessionIdentity)


def _backup(database: Path) -> Path:
    """使用 SQLite 在线备份生成一致性副本。"""
    target = database.with_name(f"{database.name}.bak-session-tables-{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
    source_conn = sqlite3.connect(database)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    return target


def _create_new_tables(database: Path) -> None:
    """按当前模型建立新表；已存在的表不会覆盖。"""
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        for model in _MODELS:
            model.__table__.create(engine, checkfirst=True)
    finally:
        engine.dispose()


def _legacy_tables(connection: sqlite3.Connection) -> dict[str, str]:
    """按已知后缀找出历史会话表，返回旧表名到新表名的映射。"""
    mapping: dict[str, str] = {}
    for (name,) in connection.execute("select name from sqlite_master where type='table'"):
        for suffix, new_name in TABLE_SUFFIXES.items():
            if name.endswith(suffix):
                mapping[name] = new_name
                break
    return mapping


def migrate_session_tables(database: Path, *, backup: bool = True) -> dict[str, object]:
    """迁移会话表；返回每张表复制行数、目标表和备份路径。

    旧表不存在时跳过；新旧表同时存在时按主键 ``INSERT OR IGNORE`` 合并后删除旧表。
    列结构不一致时直接失败，不丢弃数据。
    """
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在: {database}")
    backup_path = _backup(database) if backup else None
    _create_new_tables(database)

    copied: dict[str, int] = {}
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for old, new in _legacy_tables(connection).items():
            old_columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{old}")')]
            new_columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{new}")')]
            if set(old_columns) != set(new_columns):
                raise RuntimeError(f"表结构不一致，未迁移: {old} -> {new}")
            columns = ", ".join(f'"{name}"' for name in old_columns)
            before = connection.total_changes
            connection.execute(f'INSERT OR IGNORE INTO "{new}" ({columns}) SELECT {columns} FROM "{old}"')
            copied[old] = connection.total_changes - before
            connection.execute(f'DROP TABLE "{old}"')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "database": str(database),
        "backup": str(backup_path) if backup_path is not None else None,
        "copied": copied,
    }
