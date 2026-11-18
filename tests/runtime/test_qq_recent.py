"""QQ 群引用与近期消息缓存的旧版行为测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fxbot.core import Message, SessionTarget
from tests.runtime.test_qq import APP_ID, payload
from fxbot.platforms.qq.codec import parse_event
from fxbot.platforms.qq.client import QQClient
from fxbot.platforms.qq.recent import remember_event


class QQRecentReplyTests(unittest.IsolatedAsyncioTestCase):
    """QQ 不回推 Bot 自己发送的消息，引用时由近期缓存补全。"""

    def setUp(self) -> None:
        self.http = SimpleNamespace(request=AsyncMock(return_value={"ok": True}), put_upload_part=AsyncMock())
        self.bot = QQClient(APP_ID, self.http)

    async def test_bot_sent_group_message_is_read_from_cache_when_quoted(self) -> None:
        self.http.request.return_value = {"id": "BOT_MSG", "ext_info": {"ref_idx": "REFIDX_BOT"}}
        await self.bot.send(
            SessionTarget("qq", APP_ID, "group", "CACHE_GROUP"),
            Message.text("bot reply"),
        )

        source = payload("GROUP_MESSAGE_CREATE", content="question")
        source["d"].update(
            group_openid="CACHE_GROUP",
            message_type=103,
            message_scene={"ext": ["ref_msg_idx=REFIDX_BOT"]},
            msg_elements=[{"msg_idx": "REFIDX_BOT", "content": ""}],
        )
        event = parse_event(source, bot_id=APP_ID)
        quoted = await self.bot.get_reply(event)
        self.assertEqual(quoted.message.plain_text(), "bot reply")
        self.assertEqual(quoted.sender_id, APP_ID)

    async def test_recent_incoming_message_resolves_quote_by_msg_index(self) -> None:
        source = payload("GROUP_MESSAGE_CREATE", content="original text")
        source["d"].update(
            group_openid="CACHE_INCOMING",
            message_scene={"ext": ["msg_idx=REFIDX_ORIGINAL"]},
        )
        remember_event(self.bot, parse_event(source, bot_id=APP_ID))

        quoted_source = payload("GROUP_MESSAGE_CREATE", content="question")
        quoted_source["d"].update(
            group_openid="CACHE_INCOMING",
            message_type=103,
            message_scene={"ext": ["ref_msg_idx=REFIDX_ORIGINAL"]},
            msg_elements=[],
        )
        event = parse_event(quoted_source, bot_id=APP_ID)
        quoted = await self.bot.get_reply(event)
        self.assertEqual(quoted.message.plain_text(), "original text")
        self.assertEqual(quoted.sender_id, "MEMBER_OPENID")
        self.assertEqual(quoted.sender_name, "昵称")
