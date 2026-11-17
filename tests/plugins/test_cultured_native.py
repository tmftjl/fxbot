"""Cultured 图库插件的原生路由与更新边界回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from fxbot.core import Router, Message
from fxbot.plugins.cultured import commands as cultured, update_gallery
from fxbot.platforms.onebot11 import OneBot11Client, parse_event
from tests.migration.test_onebot11 import payload


class CulturedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = OneBot11Client("100", AsyncMock())
        self.bot.send = AsyncMock()
        self.event = parse_event(payload(), bot_id="100")

    def router(self) -> Router:
        router = Router()
        router.add(
            "api",
            cultured._handle_api_picture,
            match=cultured.api_cmd.match,
            rule=cultured.api_cmd.options.rule,
            priority=5,
            block=True,
        )
        router.add("list", cultured._handle_list, match=cultured.list_cmd.match)
        router.add(
            "picture",
            cultured._handle_picture,
            match=cultured.picture_cmd.match,
            rule=cultured.picture_cmd.options.rule,
            priority=5,
            block=True,
        )
        return router

    async def test_unknown_or_disabled_gallery_does_not_block_chat(self):
        for enabled in (True, False):
            router = self.router()
            chat = AsyncMock()
            router.message("chat", chat, priority=20, block=False)
            with (
                patch.object(cultured, "load_cfg", return_value={"random_picture_open": enabled}),
                patch.object(cultured, "load_all_commands", return_value=[]),
                patch.object(cultured, "face_list", return_value=[]),
            ):
                result = await router.dispatch(self.bot, replace(self.event, message=Message.text("看看今天的天气")))
            self.assertFalse(result.blocked)
            self.assertEqual(result.handled, ("chat",))
            chat.assert_awaited_once()
        self.bot.send.assert_not_awaited()

    async def test_api_gallery_sends_configured_picture_with_text(self) -> None:
        command = {
            "name": "jk",
            "url": "https://img.example.test/a.jpg",
            "method": "direct",
            "regex": "",
            "response_text": "来咯",
        }
        event = replace(self.event, message=Message.text("#随机jk"))
        with (
            patch.object(cultured, "load_cfg", return_value={"random_picture_open": True}),
            patch.object(cultured, "load_all_commands", return_value=[command]),
        ):
            result = await self.router().dispatch(self.bot, event)
        self.assertEqual(result.failed, ())
        sent = self.bot.send.await_args.args[1]
        self.assertEqual(sent.plain_text(), "来咯")
        self.assertEqual(sent.segments[-1].type, "image")
        self.assertEqual(sent.segments[-1].data["file"], "https://img.example.test/a.jpg")

    async def test_closed_gallery_does_not_reply(self) -> None:
        event = replace(self.event, message=Message.text("#随机jk"))
        with (
            patch.object(cultured, "load_cfg", return_value={"random_picture_open": False}),
            patch.object(cultured, "load_all_commands", return_value=[]),
        ):
            await self.router().dispatch(self.bot, event)
        self.bot.send.assert_not_awaited()

    async def test_api_gallery_takes_precedence_when_local_name_is_the_same(self):
        command = {
            "name": "jk",
            "url": "https://img.example.test/a.jpg",
            "method": "direct",
            "regex": "",
            "response_text": "",
        }
        with (
            patch.object(cultured, "load_cfg", return_value={"random_picture_open": True}),
            patch.object(cultured, "load_all_commands", return_value=[command]),
            patch.object(cultured, "face_list", return_value=["jk"]),
            patch.object(cultured, "random_local_image") as local,
        ):
            result = await self.router().dispatch(self.bot, replace(self.event, message=Message.text("#随机jk")))
        self.assertEqual(result.handled, ("api",))
        self.bot.send.assert_awaited_once()
        local.assert_not_called()

    async def test_list_and_local_random_read_from_gallery_directory(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        image = Path(directory.name) / "a.png"
        image.write_bytes(b"png")
        list_event = replace(self.event, message=Message.text("表情包列表"))
        random_event = replace(self.event, message=Message.text("#随机jk"))
        with (
            patch.object(cultured, "load_cfg", return_value={"random_picture_open": True}),
            patch.object(cultured, "face_list", return_value=["jk"]),
            patch.object(cultured, "random_local_image", return_value=image),
            patch.object(cultured, "load_all_commands", return_value=[]),
        ):
            await self.router().dispatch(self.bot, list_event)
            self.assertIn("jk", self.bot.send.await_args.args[1].plain_text())
            await self.router().dispatch(self.bot, random_event)
        self.assertEqual(self.bot.send.await_args.args[1].segments[-1].data["file"], b"png")

    async def test_history_update_is_superuser_only_and_rejects_concurrent_runs(self) -> None:
        self.assertEqual(update_gallery.update_cmd.options.level, "superuser")
        update_gallery._updating_gallery = True
        self.addCleanup(setattr, update_gallery, "_updating_gallery", False)
        router = Router()
        router.add("update", update_gallery._handle_update, match=update_gallery.update_cmd.match)
        await router.dispatch(self.bot, replace(self.event, message=Message.text("图库更新")))
        self.assertIn("已有图库更新任务", self.bot.send.await_args.args[1].plain_text())

    def test_build_picture_keeps_optional_prefix_text(self) -> None:
        message = cultured._build_picture("https://img.example.test/a.jpg", "前缀")
        self.assertEqual([segment.type for segment in message.segments], ["text", "image"])
        self.assertEqual(message.segments[-1].data["file"], "https://img.example.test/a.jpg")

    def test_http_gallery_image_is_passed_through(self) -> None:
        message = cultured._build_picture("http://gchat.qpic.cn/a.jpg", "美腿来咯~")
        self.assertEqual(message.segments[-1].data["file"], "http://gchat.qpic.cn/a.jpg")


if __name__ == "__main__":
    unittest.main()
