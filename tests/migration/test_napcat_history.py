"""显式 NapCat 历史能力的身份、分页和失败语义。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fxbot.core import Capability, SessionTarget
from fxbot.platforms.onebot11.napcat import NapCatClient, history_message
from fxbot.platforms.onebot11.protocol import OneBot11Protocol

TARGET = SessionTarget("onebot11", "100", "group", "300")


def raw(identifier: int, timestamp: int, text: str = "正文") -> dict:
    """短消息 ID 与协议消息序号不同，不能混用为分页锚点。"""
    return {
        "message_id": identifier,
        "message_seq": abs(identifier) * 10,
        "time": timestamp,
        "message_type": "group",
        "group_id": 300,
        "self_id": 100,
        "user_id": 200,
        "sender": {"user_id": 200, "nickname": "成员"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


class NapCatHistoryTests(unittest.IsolatedAsyncioTestCase):
    """全部动作由可控发送函数处理，不访问真实平台。"""

    async def test_protocol_implementation_is_explicit_not_version_detected(self) -> None:
        sender = AsyncMock()
        self.assertNotIn(Capability.HISTORY, OneBot11Protocol().create_client("100", sender).capabilities)
        client = OneBot11Protocol("napcat").create_client("100", sender)
        self.assertIsInstance(client, NapCatClient)
        self.assertIn(Capability.HISTORY, client.capabilities)
        with self.assertRaises(ValueError):
            OneBot11Protocol("guess")
        sender.assert_not_awaited()

    async def test_pages_by_explicit_message_id_and_deduplicates(self) -> None:
        sender = AsyncMock(
            side_effect=[
                {"messages": [raw(4, 4000), raw(3, 3000)]},
                {"messages": [raw(3, 3000), raw(2, 2000), raw(1, 1000)]},
            ]
        )
        messages = await NapCatClient("100", sender).fetch_group_history(
            TARGET, start_time=1500, end_time=5000, limit=10
        )
        self.assertEqual([message.message_id for message in messages], ["2", "3", "4"])
        self.assertEqual(
            sender.await_args_list[0].args,
            (
                "get_group_msg_history",
                {
                    "group_id": "300",
                    "count": 100,
                    "reverse_order": True,
                },
            ),
        )
        self.assertEqual(sender.await_args_list[1].args[1]["message_seq"], "3")
        self.assertNotIn("reverseOrder", sender.await_args_list[0].args[1])

    async def test_window_and_limit_are_applied_to_latest_messages(self) -> None:
        sender = AsyncMock(return_value={"messages": [raw(5, 5000), raw(4, 4000), raw(3, 3000), raw(2, 2000)]})
        result = await NapCatClient("100", sender).fetch_group_history(TARGET, start_time=2000, end_time=5000, limit=2)
        self.assertEqual([message.message_id for message in result], ["3", "4"])
        sender.assert_awaited_once()

    async def test_page_failure_propagates_no_retry_or_partial_statistics(self) -> None:
        sender = AsyncMock(side_effect=[{"messages": [raw(4, 4000)]}, TimeoutError("upstream")])
        with self.assertRaises(TimeoutError):
            await NapCatClient("100", sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)
        self.assertEqual(sender.await_count, 2)

    async def test_wrong_target_rejected_before_sending(self) -> None:
        sender = AsyncMock()
        for target in (
            SessionTarget("onebot11", "101", "group", "300"),
            SessionTarget("onebot12.qq", "100", "group", "300"),
            SessionTarget("onebot11", "100", "private", "300"),
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                await NapCatClient("100", sender).fetch_group_history(target, start_time=0, end_time=5000, limit=10)
        sender.assert_not_awaited()

    async def test_cross_group_or_bot_payload_never_enters_results(self) -> None:
        for changes in (
            {"group_id": 301},
            {"self_id": 101},
            {"sender": {"user_id": 201}},
            {"time": True},
            {"message_seq": "10"},
        ):
            sender = AsyncMock(return_value={"messages": [{**raw(1, 1000), **changes}]})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await NapCatClient("100", sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)

    async def test_repeated_anchor_stops_without_an_infinite_query_loop(self) -> None:
        sender = AsyncMock(return_value={"messages": [raw(1, 1000)]})
        result = await NapCatClient("100", sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)
        self.assertEqual([message.message_id for message in result], ["1"])
        self.assertEqual(sender.await_count, 2)

    async def test_page_budget_exhaustion_is_not_a_successful_partial_result(self) -> None:
        sender = AsyncMock(side_effect=[{"messages": [raw(2, 2000)]}, {"messages": [raw(1, 1000)]}])
        with patch("fxbot.platforms.onebot11.napcat.MAX_PAGES", 2), self.assertRaises(ValueError):
            await NapCatClient("100", sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)

    def test_cq_content_uses_single_protocol_parser_and_preserves_reply(self) -> None:
        message = raw(-1, 1000)
        message["message"] = "[CQ:reply,id=2]看图[CQ:image,file=a.jpg]和[CQ:face,id=1]"
        result = history_message(message, TARGET)
        self.assertEqual((result.text, result.images, result.reply_to_id), ("看图[图片]和[表情]", 1, "2"))

    async def test_missing_message_identity_is_not_replaced_with_content_hash(self) -> None:
        message = raw(1, 1000)
        message.pop("message_id")
        sender = AsyncMock(return_value={"messages": [message]})
        with self.assertRaises(KeyError):
            await NapCatClient("100", sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)
