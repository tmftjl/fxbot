"""鸣潮表情插件的原生规则、发送与缓存回归。"""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from fxbot.core import Message, HandlerContext
from fxbot.core.router import _Finished
from fxbot.core.matching import MatchResult
from fxbot.platforms.onebot11 import parse_event
from fxbot.plugins.wuwa_emoji import (
    WuwaEmojiError,
    CharacterEmptyError,
    store,
    _handle_random,
    _handle_refresh,
    random_emoji_rule,
)
from tests.migration.test_onebot11 import payload


class WuwaEmojiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.event = parse_event(payload(), bot_id="100")
        self.bot = SimpleNamespace(platform="qq", bot_id="app", send=AsyncMock())

    def context(self, text: str, platform: str = "qq") -> HandlerContext:
        bot = SimpleNamespace(platform=platform, bot_id="app", send=AsyncMock())
        event = replace(self.event, message=Message.text(text), occurred_at=datetime.now(timezone.utc))
        return HandlerContext(bot, event, MatchResult())

    async def test_rule_accepts_known_character_and_rejects_unknown(self) -> None:
        context = self.context("ww随机心月狐")
        with patch("fxbot.plugins.wuwa_emoji.lookup_character", return_value={"name": "心月狐"}):
            self.assertTrue(await random_emoji_rule(context))
        self.assertEqual(context.state["_wuwa_emoji_target"], "心月狐")

        unknown = self.context("ww随机不存在")
        with patch("fxbot.plugins.wuwa_emoji.lookup_character", return_value=None):
            self.assertFalse(await random_emoji_rule(unknown))

    async def test_rule_accepts_any_character_when_name_omitted(self) -> None:
        context = self.context("ww随机")
        self.assertTrue(await random_emoji_rule(context))
        self.assertEqual(context.state["_wuwa_emoji_target"], "")

    async def test_qq_sends_downloaded_bytes_and_onebot_uses_url(self) -> None:
        with (
            patch("fxbot.plugins.wuwa_emoji.fetch_random_image", AsyncMock(return_value="https://img.test/a.png")),
            patch("fxbot.plugins.wuwa_emoji.download_image", AsyncMock(return_value=b"image")),
        ):
            qq_context = self.context("ww随机")
            with self.assertRaises(_Finished):
                await _handle_random(qq_context)
            self.assertEqual(qq_context.bot.send.await_args.args[1].segments[0].data["file"], b"image")

            onebot_context = self.context("ww随机", platform="onebot11")
            with self.assertRaises(_Finished):
                await _handle_random(onebot_context)
            self.assertEqual(
                onebot_context.bot.send.await_args.args[1].segments[0].data["file"], "https://img.test/a.png"
            )

    async def test_empty_character_and_request_failure_reply(self) -> None:
        with patch("fxbot.plugins.wuwa_emoji.fetch_random_image", AsyncMock(side_effect=CharacterEmptyError())):
            context = self.context("ww随机心月狐")
            with self.assertRaises(_Finished):
                await _handle_random(context)
            self.assertIn("暂时没有可用的表情", context.bot.send.await_args.args[1].plain_text())

        with patch("fxbot.plugins.wuwa_emoji.fetch_random_image", AsyncMock(side_effect=WuwaEmojiError())):
            context = self.context("ww随机心月狐")
            with self.assertRaises(_Finished):
                await _handle_random(context)
            context.bot.send.assert_not_awaited()

    async def test_refresh_failure_reports_and_success_counts(self) -> None:
        with patch("fxbot.plugins.wuwa_emoji.refresh_characters", AsyncMock(side_effect=WuwaEmojiError())):
            context = self.context("ww刷新")
            with self.assertRaises(_Finished):
                await _handle_refresh(context)
            self.assertIn("刷新失败", context.bot.send.await_args.args[1].plain_text())

        with patch("fxbot.plugins.wuwa_emoji.refresh_characters", AsyncMock(return_value=[{"name": "a"}])):
            context = self.context("ww刷新")
            with self.assertRaises(_Finished):
                await _handle_refresh(context)
            self.assertIn("共 1 个角色", context.bot.send.await_args.args[1].plain_text())

    def test_disk_cache_roundtrip(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "characters.json"
        with patch.object(store, "CACHE_PATH", path):
            store._write_disk(123.0, [{"name": "心月狐", "slug": "xin"}])
            self.assertEqual(store._read_disk(), (123.0, [{"name": "心月狐", "slug": "xin"}]))


if __name__ == "__main__":
    unittest.main()
