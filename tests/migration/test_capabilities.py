"""最小客户端及显式能力提供者；核心不要求平台实现无关 API。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fxbot.core import Message, BotClient, Capability, Capabilities, SessionTarget, UnsupportedCapability
from fxbot.platforms.onebot11 import OneBot11Client
from fxbot.platforms.onebot12 import OneBot12Client
from fxbot.platforms.onebot11.extensions import OneBot11ExtendedClient


class CapabilityContractsTests(unittest.IsolatedAsyncioTestCase):
    async def test_minimal_message_client_needs_no_moderation_methods(self):
        class Minimal:
            platform = "test"
            bot_id = "bot"
            capabilities = Capabilities()
            message_transform = staticmethod(lambda target, message: message)

            async def send(self, target, message):
                return {"message_id": "sent"}

        bot = Minimal()
        self.assertIsInstance(bot, BotClient)
        self.assertFalse(hasattr(bot, "ban"))
        self.assertEqual(
            await bot.send(SessionTarget("test", "bot", "private", "user"), Message.text("x")), {"message_id": "sent"}
        )
        with self.assertRaises(UnsupportedCapability):
            bot.capabilities.require(Capability.MODERATION)

    async def test_capability_provider_can_be_separate_from_message_client(self):
        lookup = SimpleNamespace(member_name=AsyncMock(return_value="name"))
        capabilities = Capabilities().provide(Capability.MEMBER_LOOKUP, lookup)
        self.assertIs(capabilities.require(Capability.MEMBER_LOOKUP), lookup)
        self.assertEqual(await capabilities.require(Capability.MEMBER_LOOKUP).member_name("group", "user"), "name")

    async def test_registry_is_immutable_and_duplicate_binding_is_rejected(self):
        original = Capabilities()
        api = SimpleNamespace(like=AsyncMock())
        provided = original.provide(Capability.LIKE, api)
        self.assertNotIn(Capability.LIKE, original)
        self.assertIn(Capability.LIKE, provided)
        with self.assertRaises(ValueError):
            provided.provide(Capability.LIKE, api)
        with self.assertRaises(TypeError):
            provided._providers[Capability.LIKE] = object()

    async def test_unprovided_platform_methods_are_absent_and_do_not_call_transport(self):
        transport = AsyncMock()
        bot = OneBot12Client("bot", "wechat", transport)
        self.assertFalse(hasattr(bot, "ban"))
        self.assertFalse(hasattr(bot, "fetch_group_history"))
        with self.assertRaises(UnsupportedCapability):
            bot.capabilities.require(Capability.MODERATION)
        transport.assert_not_awaited()

    async def test_extended_client_provides_history_essence_and_muted_registry(self):
        sender = AsyncMock(side_effect=[{"app_name": "NapCat.Onebot"}, {}])
        standard = OneBot11Client("100", sender)
        extended = OneBot11ExtendedClient("100", sender)
        self.assertNotIn(Capability.HISTORY, standard.capabilities)
        self.assertFalse(hasattr(standard, "set_essence"))
        self.assertIn(Capability.HISTORY, extended.capabilities)
        self.assertIn(Capability.MUTED_MEMBERS, extended.capabilities)
        await extended.capabilities.require(Capability.ESSENCE).set_essence("1", enabled=True)
        self.assertEqual(sender.await_args_list[0].args, ("get_version_info", {}))
        self.assertEqual(sender.await_args_list[1].args, ("set_essence_msg", {"message_id": 1}))
