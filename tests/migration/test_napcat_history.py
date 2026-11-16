"""OneBot V11 扩展能力、历史分页方言和失败语义。"""

from __future__ import annotations

import time
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fxbot.core import Capability, SessionTarget, UnsupportedCapability
from fxbot.core.errors import ActionFailed
from fxbot.platforms.onebot11.protocol import OneBot11Protocol
from fxbot.platforms.onebot11.extensions import OneBot11ExtendedClient, history_message

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

    def client(self, sender: AsyncMock, dialect="napcat") -> OneBot11ExtendedClient:
        """预置已探测的方言，聚焦分页和校验行为。"""
        client = OneBot11ExtendedClient("100", sender)
        client._dialect = dialect
        client._dialect_expires = time.monotonic() + 600
        return client

    async def test_extended_capabilities_need_no_configuration_or_probe(self) -> None:
        sender = AsyncMock()
        client = OneBot11Protocol().create_client("100", sender)
        self.assertIsInstance(client, OneBot11ExtendedClient)
        for capability in (Capability.HISTORY, Capability.ESSENCE, Capability.MUTED_MEMBERS):
            self.assertIn(capability, client.capabilities)
        sender.assert_not_awaited()

    async def test_dialect_is_probed_lazily_and_cached(self) -> None:
        sender = AsyncMock(return_value={"app_name": "NapCat.Onebot"})
        client = OneBot11ExtendedClient("100", sender)
        self.assertEqual(await client.extension_dialect(), "napcat")
        self.assertEqual(sender.await_args_list[0].args, ("get_version_info", {}))
        sender.reset_mock()
        self.assertEqual(await client.extension_dialect(), "napcat")
        sender.assert_not_awaited()

    async def test_snownluma_uses_message_id_pagination(self) -> None:
        sender = AsyncMock(
            side_effect=[
                {"app_name": "SnowLuma"},
                {"messages": [raw(1, 1000)]},
                {"messages": []},
            ]
        )
        result = await OneBot11ExtendedClient("100", sender).fetch_group_history(
            TARGET, start_time=0, end_time=5000, limit=10
        )
        self.assertEqual([message.message_id for message in result], ["1"])
        calls = [call.args for call in sender.await_args_list]
        self.assertEqual(calls[0][0], "get_version_info")
        self.assertEqual(calls[1], ("get_group_msg_history", {"group_id": 300, "count": 100, "reverse_order": True}))
        self.assertEqual(
            calls[2], ("get_group_msg_history", {"group_id": 300, "count": 100, "reverse_order": True, "message_id": 1})
        )

    async def test_failed_probe_propagates_and_is_not_cached(self) -> None:
        sender = AsyncMock(side_effect=[RuntimeError("内部故障"), {"app_name": "SnowLuma"}])
        client = OneBot11ExtendedClient("100", sender)
        with self.assertRaisesRegex(RuntimeError, "内部故障"):
            await client.fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)
        self.assertEqual(await client.extension_dialect(), "sl")
        self.assertEqual([call.args[0] for call in sender.await_args_list], ["get_version_info"] * 2)

    async def test_standard_pagination_uses_message_seq_anchor(self) -> None:
        sender = AsyncMock(
            side_effect=[
                {"messages": [raw(3, 3000), raw(4, 4000)]},
                {"messages": [raw(1, 1000), raw(2, 2000), raw(3, 3000)]},
            ]
        )
        messages = await self.client(sender).fetch_group_history(TARGET, start_time=1500, end_time=5000, limit=10)
        self.assertEqual([message.message_id for message in messages], ["2", "3", "4"])
        self.assertEqual(
            sender.await_args_list[0].args,
            ("get_group_msg_history", {"group_id": 300, "count": 100, "reverseOrder": True}),
        )
        self.assertEqual(sender.await_args_list[1].args[1]["message_seq"], 30)

    async def test_window_and_limit_are_applied_to_latest_messages(self) -> None:
        sender = AsyncMock(return_value={"messages": [raw(2, 2000), raw(3, 3000), raw(4, 4000), raw(5, 5000)]})
        result = await self.client(sender).fetch_group_history(TARGET, start_time=2000, end_time=5000, limit=2)
        self.assertEqual([message.message_id for message in result], ["3", "4"])
        sender.assert_awaited_once()

    async def test_page_failure_propagates_no_retry_or_partial_statistics(self) -> None:
        sender = AsyncMock(side_effect=[{"messages": [raw(4, 4000)]}, TimeoutError("upstream")])
        with self.assertRaises(TimeoutError):
            await self.client(sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)
        self.assertEqual(sender.await_count, 2)

    async def test_wrong_target_rejected_before_sending(self) -> None:
        sender = AsyncMock()
        for target in (
            SessionTarget("onebot11", "101", "group", "300"),
            SessionTarget("onebot12.qq", "100", "group", "300"),
            SessionTarget("onebot11", "100", "private", "300"),
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                await self.client(sender).fetch_group_history(target, start_time=0, end_time=5000, limit=10)
        sender.assert_not_awaited()

    async def test_same_bot_new_connection_does_not_reuse_dialect(self) -> None:
        old = OneBot11ExtendedClient("100", AsyncMock(return_value={"app_name": "SnowLuma"}))
        sender = AsyncMock(return_value={"app_name": "NapCat.Onebot"})
        new = OneBot11ExtendedClient("100", sender)
        self.assertEqual(await old.extension_dialect(), "sl")
        self.assertEqual(await new.extension_dialect(), "napcat")
        sender.assert_awaited_once_with("get_version_info", {})

    async def test_concurrent_identification_sends_only_one_probe(self) -> None:
        sender = AsyncMock(return_value={"app_name": "NapCat.Onebot"})
        client = OneBot11ExtendedClient("100", sender)
        self.assertEqual(await asyncio.gather(*(client.extension_dialect() for _ in range(8))), ["napcat"] * 8)
        sender.assert_awaited_once()

    async def test_expired_success_is_probed_again(self) -> None:
        sender = AsyncMock(return_value={"app_name": "SnowLuma"})
        client = self.client(sender)
        client._dialect_expires = 0
        self.assertEqual(await client.extension_dialect(), "sl")
        sender.assert_awaited_once()

    async def test_cancelled_identification_does_not_poison_cache_or_lock(self) -> None:
        sender = AsyncMock(side_effect=[asyncio.CancelledError(), {"app_name": "NapCat.Onebot"}])
        client = OneBot11ExtendedClient("100", sender)
        with self.assertRaises(asyncio.CancelledError):
            await client.extension_dialect()
        self.assertEqual(await client.extension_dialect(), "napcat")

    async def test_unknown_implementation_does_not_send_extension_actions(self) -> None:
        for operation in (
            lambda client: client.fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10),
            lambda client: client.set_essence("1", enabled=True),
            lambda client: client.get_muted_members("300"),
        ):
            sender = AsyncMock(return_value={"app_name": "unknown"})
            with self.assertRaises(UnsupportedCapability):
                await operation(OneBot11ExtendedClient("100", sender))
            sender.assert_awaited_once_with("get_version_info", {})

    async def test_malformed_version_and_timeout_are_not_cached(self) -> None:
        for failure in ({}, {"data": {"app_name": "NapCat.Onebot"}}, TimeoutError("timeout")):
            sender = AsyncMock(side_effect=[failure, {"app_name": "NapCat.Onebot"}])
            client = OneBot11ExtendedClient("100", sender)
            with self.assertRaises((ActionFailed, TimeoutError)):
                await client.extension_dialect()
            self.assertEqual(await client.extension_dialect(), "napcat")

    async def test_same_second_messages_use_protocol_order_not_message_id_order(self) -> None:
        sender = AsyncMock(
            side_effect=[
                {"messages": [raw(7, 1000), raw(2, 1000), raw(4, 2000)]},
                {"messages": [raw(9, 1000), raw(7, 1000)]},
                {"messages": [raw(5, 500), raw(9, 1000)]},
                {"messages": []},
            ]
        )
        result = await self.client(sender).fetch_group_history(TARGET, start_time=0, end_time=3000, limit=10)
        self.assertEqual({message.message_id for message in result}, {"7", "2", "4", "9", "5"})
        self.assertEqual([call.args[1].get("message_seq") for call in sender.await_args_list], [None, 70, 90, 50])

    async def test_nonprogressing_multi_message_page_is_not_partial_success(self) -> None:
        sender = AsyncMock(return_value={"messages": [raw(1, 1000), raw(2, 1000)]})
        with self.assertRaisesRegex(ActionFailed, "锚点未前进"):
            await self.client(sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)

    async def test_missing_or_invalid_anchor_is_not_guessed(self) -> None:
        for value in (None, True, "", "invalid", "--1", "１２"):
            sender = AsyncMock(return_value={"messages": [{**raw(1, 1000), "message_seq": value}]})
            with self.assertRaises(ActionFailed):
                await self.client(sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)

    async def test_cross_group_or_bot_payload_never_enters_results(self) -> None:
        for changes in (
            {"group_id": 301},
            {"self_id": 101},
            {"sender": {"user_id": 201}},
            {"time": True},
        ):
            sender = AsyncMock(return_value={"messages": [{**raw(1, 1000), **changes}]})
            with self.subTest(changes=changes), self.assertRaises(ActionFailed):
                await self.client(sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)

    async def test_repeated_anchor_stops_without_an_infinite_query_loop(self) -> None:
        sender = AsyncMock(return_value={"messages": [raw(1, 1000)]})
        result = await self.client(sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)
        self.assertEqual([message.message_id for message in result], ["1"])
        self.assertEqual(sender.await_count, 2)

    async def test_page_budget_exhaustion_is_not_a_successful_partial_result(self) -> None:
        sender = AsyncMock(side_effect=[{"messages": [raw(2, 2000)]}, {"messages": [raw(1, 1000)]}])
        with patch("fxbot.platforms.onebot11.extensions.MAX_PAGES", 2), self.assertRaises(ActionFailed):
            await self.client(sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)

    def test_cq_content_uses_single_protocol_parser_and_preserves_reply(self) -> None:
        message = raw(-1, 1000)
        message["message"] = "[CQ:reply,id=2]看图[CQ:image,file=a.jpg]和[CQ:face,id=1]"
        result = history_message(message, TARGET)
        self.assertEqual((result.text, result.images, result.reply_to_id), ("看图[图片]和[表情]", 1, "2"))

    async def test_missing_message_identity_is_not_replaced_with_content_hash(self) -> None:
        message = raw(1, 1000)
        message.pop("message_id")
        sender = AsyncMock(return_value={"messages": [message]})
        with self.assertRaises(ActionFailed):
            await self.client(sender).fetch_group_history(TARGET, start_time=0, end_time=5000, limit=10)

    async def test_known_product_family_names_and_unknown_names(self) -> None:
        for name, expected in (
            ("NapCat.Onebot", "napcat"),
            ("NapCat", "napcat"),
            ("SnowLuma", "sl"),
            ("snowluma.pro", "sl"),
            ("sl", "sl"),
        ):
            with self.subTest(name=name):
                client = OneBot11ExtendedClient("100", AsyncMock(return_value={"app_name": name}))
                self.assertEqual(await client.extension_dialect(), expected)
        for name in ("notnapcat", "fake-snowluma", "napcatish"):
            with self.subTest(name=name), self.assertRaises(UnsupportedCapability):
                await OneBot11ExtendedClient("100", AsyncMock(return_value={"app_name": name})).extension_dialect()

    async def test_history_pages_are_normalized_without_assuming_array_direction(self) -> None:
        for dialect in ("napcat", "sl"):
            sender = AsyncMock(
                side_effect=[
                    {"messages": [raw(3, 3000), raw(2, 2000), raw(4, 4000)]},
                    {"messages": [raw(2, 2000), raw(1, 1000)]},
                    {"messages": []},
                ]
            )
            result = await self.client(sender, dialect).fetch_group_history(
                TARGET, start_time=0, end_time=5000, limit=10
            )
            self.assertEqual([message.message_id for message in result], ["1", "2", "3", "4"])
            key = "message_id" if dialect == "sl" else "message_seq"
            self.assertEqual(sender.await_args_list[1].args[1][key], 2 if dialect == "sl" else 20)

    async def test_empty_or_invalid_version_response_is_a_query_failure(self) -> None:
        for value in ({}, [], {"app_name": " "}, {"app_name": None}):
            client = OneBot11ExtendedClient("100", AsyncMock(return_value=value))
            with self.subTest(value=value), self.assertRaises(ActionFailed):
                await client.extension_dialect()
            self.assertIsNone(client._dialect)

    async def test_invalid_history_response_is_a_query_failure(self) -> None:
        for value in ({}, [], {"messages": None}, {"messages": [None]}, {"messages": [{}]}):
            with self.subTest(value=value), self.assertRaises(ActionFailed):
                await self.client(AsyncMock(return_value=value)).fetch_group_history(
                    TARGET, start_time=0, end_time=5000, limit=10
                )
