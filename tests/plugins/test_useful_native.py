"""实用工具插件的原生命令、图片来源与评分边界回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from fxbot.core import Router, Message, ReplyTarget, QuotedMessage, MessageSegment
from fxbot.plugins.useful import dps_chart, cos_upload, waves_analyze
from fxbot.platforms.onebot11 import OneBot11Client, parse_event
from fxbot.plugins.useful.media import image_sources
from tests.migration.test_onebot11 import payload


class UsefulTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = OneBot11Client("100", AsyncMock())
        self.bot.send = AsyncMock()
        self.event = parse_event(payload(), bot_id="100")

    async def test_dps_chart_sends_stored_bytes(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        image = Path(directory.name) / "wwdps.png"
        image.write_bytes(b"dps-image")
        router = Router()
        router.add("dps", dps_chart._handle_wwdps, match=dps_chart.wwdps_cmd.match)
        with patch.object(dps_chart, "DPS_IMAGE_PATH", image):
            await router.dispatch(self.bot, replace(self.event, message=Message.text("wwdps")))
        self.assertEqual(self.bot.send.await_args.args[1].segments[0].data["file"], b"dps-image")

    async def test_dps_missing_image_replies_with_hint(self) -> None:
        router = Router()
        router.add("dps", dps_chart._handle_wwdps, match=dps_chart.wwdps_cmd.match)
        with patch.object(dps_chart, "DPS_IMAGE_PATH", Path("missing-file.png")):
            await router.dispatch(self.bot, replace(self.event, message=Message.text("wwdps")))
        self.assertIn("尚未设置", self.bot.send.await_args.args[1].plain_text())

    async def test_image_sources_read_current_message_and_quote(self) -> None:
        event = replace(
            self.event,
            message=Message([MessageSegment.image("https://img.example.test/current.jpg")]),
            reply=ReplyTarget("m1"),
        )
        quoted = QuotedMessage(
            "m1", "200", "成员", Message([MessageSegment.image("https://img.example.test/quoted.jpg")])
        )
        self.bot.get_reply = AsyncMock(return_value=quoted)
        sources = await image_sources(self.bot, event)
        self.assertEqual(
            sources,
            ["https://img.example.test/current.jpg", "https://img.example.test/quoted.jpg"],
        )

    async def test_score_command_requires_configured_service(self) -> None:
        with patch.object(waves_analyze, "cfg_waves_analyze", return_value={"api_url": "", "token": ""}):
            image, tip = await waves_analyze._post_score([], "土豆 1c")
        self.assertIsNone(image)
        self.assertIn("未配置", tip)

    def test_command_string_and_cos_filename(self) -> None:
        self.assertEqual(waves_analyze._build_command_str(" 土豆|1cost  "), "土豆 1cost")
        self.assertTrue(cos_upload._image_filename("https://a.test/b.webp?x=1").endswith(".webp"))
        self.assertTrue(cos_upload._image_filename("https://a.test/b").endswith(".jpg"))

    def test_permission_levels_match_legacy(self) -> None:
        self.assertEqual(waves_analyze.waves_analyze_cmd.options.level, "member")
        self.assertEqual(dps_chart.update_wwdps_cmd.options.level, "all")
        self.assertEqual(cos_upload.cos_upload_cmd.options.level, "all")


if __name__ == "__main__":
    unittest.main()
