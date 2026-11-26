"""撤回通知的操作者解析与墓碑落库回归。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sqlmodel import select

from fxbot.db import base_models
from fxbot.core import Event, SessionTarget
from fxbot.chat.recall import recall_operator
from fxbot.db.base_models import init_database, close_database
from fxbot.chat.memory.models import ChatMessage, ChatRecallTombstone
from fxbot.chat.memory.repository import chat_repository


def recall_event(user_id: str, metadata: dict, *, kind: str = "private", target_id: str | None = None) -> Event:
    """构造只带撤回所需字段的通知事件。"""
    target = SessionTarget("onebot11", "10000", kind, target_id or user_id)
    return Event(
        None,
        "1422288236",
        "message_deleted",
        "onebot11",
        "10000",
        user_id,
        "",
        target,
        metadata=metadata,
        occurred_at=datetime.now(timezone.utc),
    )


class RecallOperatorTests(unittest.TestCase):
    """撤回者由会话类型确定，不把 None 传给数据库。"""

    def test_group_recall_uses_operator_id(self) -> None:
        event = recall_event(
            "111", {"notice_type": "group_recall", "operator_id": "222"}, kind="group", target_id="999"
        )
        self.assertEqual(recall_operator(event), "222")

    def test_private_recall_operator_is_the_message_author(self) -> None:
        event = recall_event("111", {"notice_type": "friend_recall"})
        self.assertEqual(recall_operator(event), "111")

    def test_group_recall_without_operator_id_is_unknown(self) -> None:
        event = recall_event("111", {"notice_type": "group_recall"}, kind="group", target_id="999")
        self.assertEqual(recall_operator(event), "")


class RecallTombstoneTests(unittest.IsolatedAsyncioTestCase):
    """墓碑与消息删除在 operator 为 None 时仍能提交。"""

    async def asyncSetUp(self) -> None:
        await init_database()
        self.addAsyncCleanup(close_database)
        self.tenant = "onebot11:10000:private:111"

    async def test_missing_operator_keeps_tombstone(self) -> None:
        result = await chat_repository.invalidate_message(self.tenant, "m-null", operator=None)  # type: ignore[arg-type]
        self.assertIsNone(result)
        async with base_models.async_maker() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChatRecallTombstone).where(ChatRecallTombstone.platform_message_id == "m-null")
                    )
                ).scalars()
            )
        self.assertEqual([row.delete_operator for row in rows], [""])

    async def test_missing_operator_deletes_existing_message(self) -> None:
        async with base_models.async_maker() as session:
            session.add(ChatMessage(tenant=self.tenant, platform_message_id="m-known", dedupe_key="msg:m-known"))
            await session.commit()
        result = await chat_repository.invalidate_message(self.tenant, "m-known", operator=None)  # type: ignore[arg-type]
        self.assertIsNotNone(result)
        async with base_models.async_maker() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChatMessage).where(ChatMessage.platform_message_id == "m-known")
                    )
                ).scalars()
            )
            tombstones = list(
                (
                    await session.execute(
                        select(ChatRecallTombstone).where(ChatRecallTombstone.platform_message_id == "m-known")
                    )
                ).scalars()
            )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].deleted)
        self.assertEqual(rows[0].delete_operator, "")
        self.assertEqual(tombstones, [])


if __name__ == "__main__":
    unittest.main()
