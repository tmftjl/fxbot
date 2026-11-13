"""QQ 事件和配置契约可在没有第三方包的解释器中使用。"""

from __future__ import annotations

import unittest

from fxbot.core import SessionTarget
from fxbot.platforms.qq.codec import opaque_id, identifier, parse_event
from fxbot.platforms.qq.config import QQBotSettings


class QQCodecIndependenceTests(unittest.TestCase):
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
        with self.assertRaises(ValueError):
            QQBotSettings.parse({"secret": "x", "verify": False})

    def test_identity_and_message_id_have_distinct_validation(self):
        self.assertEqual(opaque_id("base64/id=1:event"), "base64/id=1:event")
        with self.assertRaises(ValueError):
            identifier("../target")
        with self.assertRaises(ValueError):
            opaque_id("invalid\nid")
