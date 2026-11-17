"""原生平台的通知、身份和能力编码测试。"""

from __future__ import annotations

import ast
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

from fxbot.core import Message, SessionTarget, MessageSegment, UnsupportedCapability
from fxbot.platforms.onebot11 import OneBot11Client, parse_event as parse_v11
from fxbot.platforms.onebot12 import OneBot12Client, parse_event as parse_v12, encode_message
from fxbot.platforms.onebot12.protocol import OneBot12Protocol


def v12_payload() -> dict:
    """创建有独立平台身份的 V12 测试帧。"""
    return {
        "id": "event",
        "time": 1.5,
        "self": {"platform": "wechat", "user_id": "bot"},
        "type": "message",
        "detail_type": "group",
        "sub_type": "",
        "group_id": "group",
        "user_id": "user",
        "message_id": "message",
        "message": [{"type": "text", "data": {"text": "/ping"}}],
    }


class PlatformContractTests(unittest.TestCase):
    """平台只能依赖核心，核心不能反向导入平台。"""

    def test_core_does_not_import_platforms_or_framework(self) -> None:
        root = Path(__file__).resolve().parents[2] / "src/fxbot"
        self.assertFalse((root / "core/platforms/__init__.py").exists())
        self.assertIsNone(importlib.util.find_spec("fxbot.core.platforms"))
        for source in (root / "core").glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("platforms", (node.module or "").split("."), str(source))

    def test_v11_recall_and_membership_notifications(self) -> None:
        for notice, expected in (("group_recall", "message_deleted"), ("group_increase", "member_joined")):
            event = parse_v11(
                {
                    "self_id": 100,
                    "time": 1,
                    "post_type": "notice",
                    "notice_type": notice,
                    "user_id": 200,
                    "group_id": 300,
                    "operator_id": 100,
                    "message_id": -5,
                },
                bot_id="100",
            )
            self.assertEqual(event.event_type, expected)
            self.assertEqual(event.metadata["operator_id"], "100")
            self.assertEqual(event.target.target_id, "300")

    def test_v12_namespace_separates_actual_platforms(self) -> None:
        event = parse_v12(v12_payload(), bot_id="bot", platform="wechat")
        self.assertEqual(event.target.platform, "onebot12.wechat")
        self.assertEqual(event.event_id, "event")
        self.assertEqual(event.message_id, "message")
        with self.assertRaises(ValueError):
            parse_v12(v12_payload(), bot_id="bot", platform="qq")

    def test_v12_recall(self) -> None:
        payload = {**v12_payload(), "type": "notice", "detail_type": "group_message_delete", "operator_id": "admin"}
        event = parse_v12(payload, bot_id="bot", platform="wechat")
        self.assertEqual(event.event_type, "message_deleted")
        self.assertEqual(event.metadata["operator_id"], "admin")

    def test_v12_media_requires_real_file_ids(self) -> None:
        self.assertEqual(
            encode_message(Message([MessageSegment("image", {"file_id": "image"})])),
            [
                {"type": "image", "data": {"file_id": "image"}},
            ],
        )
        with self.assertRaises(ValueError):
            encode_message(Message([MessageSegment("image", {"file": "https://example.test"})]))
        with self.assertRaises(UnsupportedCapability):
            encode_message(Message([MessageSegment("onebot12:other")]))

    def test_v12_action_envelope_contains_self_identity(self) -> None:
        protocol = OneBot12Protocol("wechat")
        packet = protocol.encode_action({"action": "get_status", "params": {}, "echo": "id"}, "bot")
        self.assertEqual(packet["self"], {"platform": "wechat", "user_id": "bot"})


class ClientCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """动作使用明确协议参数，无探测失败后的参数重试。"""

    async def test_target_names_follow_each_protocols_defined_fields(self) -> None:
        v11 = OneBot11Client("100", AsyncMock(return_value={"user_id": 200, "nickname": "QQ 用户"}))
        self.assertEqual(await v11.target_name(SessionTarget("onebot11", "100", "private", "200")), "QQ 用户")
        action = AsyncMock(return_value={"user_id": "user", "user_name": "用户", "user_displayname": "显示名"})
        v12 = OneBot12Client("bot", "wechat", action)
        self.assertEqual(await v12.target_name(SessionTarget(v12.platform, "bot", "private", "user")), "用户")
        self.assertEqual(await v12.member_name("group", "user"), "显示名")
        action.return_value = {"group_id": "group", "group_name": "群名"}
        self.assertEqual(await v12.target_name(SessionTarget(v12.platform, "bot", "group", "group")), "群名")

    async def test_target_lookup_rejects_wrong_connection_and_wrong_response_identity(self) -> None:
        action = AsyncMock(return_value={"group_id": "other", "group_name": "其他群"})
        v12 = OneBot12Client("bot", "wechat", action)
        with self.assertRaises(ValueError):
            await v12.target_name(SessionTarget("onebot12.qq", "bot", "group", "group"))
        action.assert_not_awaited()
        with self.assertRaises(ValueError):
            await v12.target_name(SessionTarget(v12.platform, "bot", "group", "group"))

    async def test_v11_admin_actions(self) -> None:
        sender = AsyncMock(return_value={})
        client = OneBot11Client("100", sender)
        await client.ban("300", "200", 60)
        sender.assert_awaited_with("set_group_ban", {"group_id": 300, "user_id": 200, "duration": 60})
        await client.delete_message("-1")
        sender.assert_awaited_with("delete_msg", {"message_id": -1})
        with self.assertRaises(ValueError):
            await client.ban("300", "200", True)

    async def test_v12_send_and_lookup(self) -> None:
        sender = AsyncMock(return_value={})
        client = OneBot12Client("bot", "wechat", sender)
        await client.send(SessionTarget(client.platform, "bot", "group", "group"), Message.text("hi"))
        sender.assert_awaited_with(
            "send_message",
            {
                "detail_type": "group",
                "group_id": "group",
                "message": [{"type": "text", "data": {"text": "hi"}}],
            },
        )
        await client.get_member("group", "user")
        sender.assert_awaited_with("get_group_member_info", {"group_id": "group", "user_id": "user"})
