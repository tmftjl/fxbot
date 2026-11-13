"""协议事件解析、消息编码和命令收发测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import AsyncMock

from fxbot.core import Message, SessionTarget, MessageSegment, UnsupportedCapability, current_event
from fxbot.core.router import Router
from fxbot.platforms.onebot11 import OneBot11Client, parse_event, parse_message, encode_message


def payload(kind: str = "group") -> dict:
    """创建不含外部账号的协议消息。"""
    return {
        "time": 123,
        "self_id": 100,
        "post_type": "message",
        "message_type": kind,
        "sub_type": "normal" if kind == "group" else "friend",
        "message_id": -42,
        "user_id": 200,
        "group_id": 300,
        "message": [{"type": "text", "data": {"text": "/ping"}}],
        "sender": {"user_id": 200, "nickname": "昵称", "card": "群名片", "role": "admin"},
    }


class ParseTests(unittest.TestCase):
    """协议数据必须独立复制并绑定明确身份。"""

    def test_group_identity_and_negative_message_id(self) -> None:
        event = parse_event(payload(), bot_id="100")
        self.assertIsNone(event.event_id)
        self.assertEqual(event.message_id, "-42")
        self.assertEqual(event.target, SessionTarget("onebot11", "100", "group", "300"))
        self.assertEqual(event.user_name, "群名片")
        self.assertEqual(event.metadata["sender_role"], "admin")
        self.assertFalse(event.to_me)

    def test_private_target_is_sender_not_extra_group_field(self) -> None:
        event = parse_event(payload("private"), bot_id="100")
        self.assertEqual(event.target.kind, "private")
        self.assertEqual(event.target.target_id, "200")
        self.assertTrue(event.to_me)

    def test_mentions_reply_and_original_message_are_distinct_copies(self) -> None:
        data = payload()
        data["message"] = [
            {"type": "at", "data": {"qq": "100"}},
            {"type": "text", "data": {"text": " /ping"}},
            {"type": "reply", "data": {"id": "-123"}},
        ]
        before = deepcopy(data)
        event = parse_event(data, bot_id="100")
        self.assertTrue(event.to_me)
        self.assertEqual(event.reply.message_id, "-123")
        self.assertEqual(event.message.segments[0].type, "text")
        self.assertEqual(event.original_message.segments[0].type, "mention")
        event.message.segments[0].data["text"] = "changed"
        self.assertEqual(event.original_message.plain_text(), " /ping")
        self.assertEqual(data, before)

    def test_segment_types_and_unknown_data(self) -> None:
        message = parse_message(
            [
                {"type": "at", "data": {"qq": "all"}},
                {"type": "record", "data": {"file": "https://example.com/a"}},
                {"type": "face", "data": {"id": "1"}},
            ]
        )
        self.assertEqual([s.type for s in message.segments], ["mention_all", "voice", "onebot11:face"])
        self.assertEqual(message.mentions(), [])

    def test_wrong_identity_and_malformed_ids_are_rejected(self) -> None:
        for field, value in (("self_id", 999), ("user_id", True), ("group_id", "3:00"), ("message_id", None)):
            with self.subTest(field=field):
                data = payload()
                data[field] = value
                with self.assertRaises(ValueError):
                    parse_event(data, bot_id="100")

    def test_inconsistent_sender_is_rejected(self) -> None:
        data = payload()
        data["sender"]["user_id"] = 999
        with self.assertRaises(ValueError):
            parse_event(data, bot_id="100")

    def test_requests_are_explicitly_unsupported(self) -> None:
        data = payload()
        data["post_type"] = "request"
        with self.assertRaises(UnsupportedCapability):
            parse_event(data, bot_id="100")

    def test_cq_string_is_parsed_once_and_escaped_text_stays_text(self) -> None:
        message = parse_message("&#91;CQ:at,qq=999&#93;[CQ:at,qq=100]a&amp;b")
        self.assertEqual(message.mentions(), ["100"])
        self.assertEqual(message.plain_text(), "[CQ:at,qq=999]a&b")

    def test_malformed_segments_reject_runtime_objects(self) -> None:
        for value in ([{}], [{"type": "text", "data": {"text": object()}}], [{"type": "text", "data": []}]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_message(value)

    def test_malformed_metadata_is_rejected(self) -> None:
        for field, value in (("time", True), ("time", -1), ("sender", None), ("sub_type", object())):
            data = payload()
            data[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    parse_event(data, bot_id="100")

    def test_anonymous_identity_is_explicitly_unsupported(self) -> None:
        data = payload()
        data["sub_type"] = "anonymous"
        with self.assertRaises(UnsupportedCapability):
            parse_event(data, bot_id="100")

    def test_nonfinite_numbers_are_rejected(self) -> None:
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                parse_message([{"type": "face", "data": {"id": value}}])

    def test_encode_text_as_array_and_translate_supported_segments(self) -> None:
        message = Message(
            [
                MessageSegment.text("[CQ:at,qq=999]"),
                MessageSegment.mention("200"),
                MessageSegment("mention_all"),
                MessageSegment("reply", {"message_id": "-1"}),
                MessageSegment("audio", {"file": "file:///voice.wav"}),
            ]
        )
        result = encode_message(message)
        self.assertEqual(result[0], {"type": "text", "data": {"text": "[CQ:at,qq=999]"}})
        self.assertEqual(result[1], {"type": "at", "data": {"qq": "200"}})
        self.assertEqual(result[2]["data"], {"qq": "all"})
        self.assertEqual(result[3]["data"], {"id": "-1"})
        self.assertEqual(result[4]["type"], "record")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    """发送前检查身份和能力，动作只由注入函数传输。"""

    async def test_group_and_private_actions(self) -> None:
        sender = AsyncMock(return_value={"message_id": 88})
        client = OneBot11Client("100", sender)
        for kind, key, action in (("group", "group_id", "send_group_msg"), ("private", "user_id", "send_private_msg")):
            target = SessionTarget("onebot11", "100", kind, "200")
            result = await client.send(target, Message.text("hello"))
            self.assertEqual(result, {"message_id": 88})
            sender.assert_awaited_with(action, {key: 200, "message": [{"type": "text", "data": {"text": "hello"}}]})

    async def test_invalid_targets_never_reach_sender(self) -> None:
        sender = AsyncMock()
        client = OneBot11Client("100", sender)
        targets = [
            SessionTarget("other", "100", "group", "200"),
            SessionTarget("onebot11", "999", "group", "200"),
            SessionTarget("onebot11", "100", "channel", "200"),
            SessionTarget("onebot11", "100", "group", "oops"),
        ]
        for target in targets:
            with self.assertRaises((ValueError, UnsupportedCapability)):
                await client.send(target, Message.text("hello"))
        sender.assert_not_awaited()

    async def test_invalid_and_unsupported_segments_never_reach_sender(self) -> None:
        sender = AsyncMock()
        client = OneBot11Client("100", sender)
        target = SessionTarget("onebot11", "100", "group", "200")
        for segment in (
            MessageSegment("onebot11:face", {"id": "1"}),
            MessageSegment("image"),
            MessageSegment("text", {"text": None}),
        ):
            with self.assertRaises((ValueError, UnsupportedCapability)):
                await client.send(target, Message([segment]))
        sender.assert_not_awaited()

    async def test_transport_failure_is_not_converted_to_success(self) -> None:
        sender = AsyncMock(side_effect=OSError("connection closed"))
        client = OneBot11Client("100", sender)
        with self.assertRaises(OSError):
            await client.send(SessionTarget("onebot11", "100", "private", "200"), Message.text("hello"))

    async def test_raw_event_to_command_and_outbound_action(self) -> None:
        sender = AsyncMock(return_value={"message_id": 99})
        client = OneBot11Client("100", sender)
        router = Router()

        async def ping(context):
            self.assertIs(current_event(), context.event)
            await context.finish("pong")
            self.fail("finish 后不能继续执行")

        router.command("ping", "/ping", ping)
        result = await router.dispatch(client, parse_event(payload(), bot_id="100"))
        self.assertEqual(result.handled, ("ping",))
        self.assertTrue(result.blocked)
        self.assertEqual(result.failed, ())
        sender.assert_awaited_once_with(
            "send_group_msg", {"group_id": 300, "message": [{"type": "text", "data": {"text": "pong"}}]}
        )
        with self.assertRaises(RuntimeError):
            current_event()


if __name__ == "__main__":
    unittest.main()
