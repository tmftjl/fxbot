"""QQ 事件字段契约在独立运行时中的映射。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fxbot.core import SessionTarget
from fxbot.platforms.qq.codec import parse_event
from fxbot.platforms.qq.config import QQBotSettings


class QQCodecIndependenceTests(unittest.TestCase):
    def test_member_join_unix_seconds_are_normalized_to_utc(self):
        frame = {
            "op": 0,
            "t": "GROUP_MEMBER_ADD",
            "id": "joined",
            "d": {
                "group_openid": "GROUP",
                "member_openid": "MEMBER",
                "timestamp": 1784276757,
            },
        }
        event = parse_event(frame, bot_id="123")
        self.assertEqual(event.event_type, "member_joined")
        self.assertEqual(event.occurred_at, datetime.fromtimestamp(1784276757, timezone.utc))
        self.assertEqual(event.metadata["qq_data"]["timestamp"], 1784276757)

    def test_notice_rejects_invalid_unix_seconds_and_accepts_zoned_iso(self):
        frame = {
            "op": 0,
            "t": "GROUP_MEMBER_ADD",
            "id": "joined",
            "d": {
                "group_openid": "GROUP",
                "member_openid": "MEMBER",
                "timestamp": 0,
            },
        }
        for value in (True, False, None, "not-a-time"):
            frame["d"]["timestamp"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_event(frame, bot_id="123")
        for value in (0, 1.5, -1, 1784276757, "1784276757", "2026-11-16T10:00:00", "2026-11-16T10:00:00+08:00"):
            frame["d"]["timestamp"] = value
            self.assertIsNotNone(parse_event(frame, bot_id="123").occurred_at.tzinfo)

    def test_openid_is_not_converted_to_qq_number(self):
        payload = {
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "id": "C2C:event",
            "d": {
                "id": "opaque/message=id",
                "content": "hello",
                "timestamp": "2026-11-13T00:00:00+08:00",
                "author": {"user_openid": "OPENID", "id": "not-user-openid"},
            },
        }
        event = parse_event(payload, bot_id="123")
        self.assertEqual(event.target, SessionTarget("qq", "123", "private", "OPENID"))
        self.assertEqual(event.event_id, "C2C:event")
        self.assertEqual(event.message_id, "opaque/message=id")
        self.assertEqual(event.message.plain_text(), "hello")

    def test_credentials_not_exposed_by_repr(self):
        config = QQBotSettings("never-print-secret")
        self.assertNotIn("never-print-secret", repr(config))
        self.assertFalse(QQBotSettings.parse({"secret": "x", "verify_webhook": False}).verify_webhook)

    def test_ids_have_no_extra_format_length_or_nonempty_constraint(self):
        for value in ("", "../target", "invalid\nid", "身份" * 1024):
            event = parse_event(
                {
                    "op": 0,
                    "t": "GROUP_MEMBER_REMOVE",
                    "id": value,
                    "d": {
                        "group_openid": value,
                        "member_openid": value,
                        "timestamp": 0,
                    },
                },
                bot_id="app",
            )
            self.assertEqual(event.event_id, value)
            self.assertEqual(event.user_id, value)
            self.assertEqual(event.target.target_id, value)
