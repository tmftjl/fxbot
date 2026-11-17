"""会话身份持久化表。

表情调用记录只保存 ``session_persist_id``，统计需要按 Bot/Scene/User/Session
关联查询。这里的表名和列与新运行时一致，旧库由迁移脚本一次性改名。
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Field, SQLModel
from sqlalchemy import JSON, Column, UniqueConstraint


class SessionBot(SQLModel, table=True):
    """持久化 Bot 身份。"""

    __tablename__ = "session_bot"
    __table_args__ = (UniqueConstraint("self_id", "adapter", name="session_bot_unique"),)

    id: int | None = Field(default=None, primary_key=True)
    self_id: str = Field(max_length=64, nullable=False)
    adapter: str = Field(max_length=32, nullable=False)
    scope: str = Field(max_length=32, nullable=False)


class SessionScene(SQLModel, table=True):
    """持久化会话场景。"""

    __tablename__ = "session_scene"
    __table_args__ = (UniqueConstraint("bot_persist_id", "scene_id", "scene_type", name="session_scene_unique"),)

    id: int | None = Field(default=None, primary_key=True)
    bot_persist_id: int = Field(nullable=False)
    parent_scene_persist_id: int | None = Field(default=None, nullable=True)
    scene_id: str = Field(max_length=64, nullable=False)
    scene_type: int = Field(nullable=False)
    scene_data: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class SessionUser(SQLModel, table=True):
    """持久化用户。"""

    __tablename__ = "session_user"
    __table_args__ = (UniqueConstraint("bot_persist_id", "user_id", name="session_user_unique"),)

    id: int | None = Field(default=None, primary_key=True)
    bot_persist_id: int = Field(nullable=False)
    user_id: str = Field(max_length=64, nullable=False)
    user_data: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class SessionIdentity(SQLModel, table=True):
    """持久化会话。"""

    __tablename__ = "session_identity"
    __table_args__ = (
        UniqueConstraint(
            "bot_persist_id",
            "scene_persist_id",
            "user_persist_id",
            name="session_identity_unique",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    bot_persist_id: int = Field(nullable=False)
    scene_persist_id: int = Field(nullable=False)
    user_persist_id: int = Field(nullable=False)
    member_data: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
