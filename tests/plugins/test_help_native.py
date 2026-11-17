"""原生帮助插件的配置解析、命令匹配、兜底渲染与缓存。"""

from __future__ import annotations

import sys
import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fxbot.core import Event, Message, SessionTarget
from fxbot.plugins import help as help_plugin
from fxbot.core.router import HandlerContext, _Finished
from fxbot.utils.paths import package_root
from fxbot.plugins.help import config as help_config

TARGET = SessionTarget("onebot11", "100", "group", "300")


class _Bot:
    platform = "onebot11"
    bot_id = "100"

    def __init__(self) -> None:
        self.sent: list[tuple[SessionTarget, Message]] = []

    async def send(self, target: SessionTarget, message: Message):
        self.sent.append((target, message))
        return {"message_id": 1}


def _event(text: str) -> Event:
    return Event(
        None,
        None,
        "message",
        "onebot11",
        "100",
        "200",
        "用户",
        TARGET,
        Message.text(text),
    )


def _context(bot: _Bot, text: str) -> HandlerContext:
    event = _event(text)
    match = help_plugin.help_cmd.match(event)
    assert match is not None
    return HandlerContext(bot, event, match)


class HelpConfigTests(unittest.TestCase):
    def test_default_config_comes_from_bundled_resources(self) -> None:
        self.assertEqual(help_config.PLUGIN_DIR, package_root() / "plugins")
        ref = help_config.default_help_config()
        self.assertEqual(ref.key, "help")
        self.assertEqual(ref.path, help_config.CFG_DIR / "help.json")
        self.assertTrue(ref.path.is_file())

        config = help_config.load_help_config(None)
        self.assertEqual(config["title"], "今汐机器人帮助")
        self.assertEqual(config["footer"], "dggb 强力驱动")
        self.assertEqual(config["col_count"], 3)
        self.assertEqual(len(config["groups"]), 4)
        self.assertEqual(config["_config_key"], "help")
        self.assertEqual(config["_base_dir"], str(ref.path.parent))

    def test_keywords_and_aliases_resolve_case_insensitively(self) -> None:
        cases = {
            "群管": "群管",
            "GROUP_ADMIN": "群管",
            " Admin ": "群管",
            "bilibili_live": "b站直播",
            "B站直播推送": "b站直播",
            "群聊分析": "群聊日报",
            "群聊日报": "群聊日报",
        }
        for keyword, expected_key in cases.items():
            with self.subTest(keyword=keyword):
                ref = help_config.resolve_help_config(keyword)
                self.assertIsNotNone(ref)
                self.assertEqual(ref.key, expected_key)
                self.assertTrue(ref.path.is_file())

    def test_blank_keyword_uses_default_and_unknown_is_none(self) -> None:
        self.assertEqual(help_config.resolve_help_config(""), help_config.default_help_config())
        self.assertEqual(help_config.resolve_help_config("   "), help_config.default_help_config())
        self.assertIsNone(help_config.resolve_help_config("绝不存在的帮助关键词"))

    def test_scan_skips_self_missing_and_broken_configs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("alpha", "beta", "gamma", "help"):
                (root / name).mkdir()
            (root / "alpha" / "help.json").write_text(
                json.dumps({"key": "Alpha", "aliases": ["A"]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "gamma" / "help.json").write_text("{bad", encoding="utf-8")
            (root / "help" / "help.json").write_text('{"key": "help"}', encoding="utf-8")
            with patch.object(help_config, "PLUGIN_DIR", root):
                mapping = help_config._scan_plugin_configs()
        self.assertEqual(mapping["alpha"].key, "alpha")
        self.assertEqual(mapping["a"].key, "alpha")
        self.assertNotIn("beta", mapping)
        self.assertNotIn("gamma", mapping)
        self.assertNotIn("help", mapping)


class HelpMatcherTests(unittest.TestCase):
    def test_command_regex_captures_keyword(self) -> None:
        cases = {
            "#帮助": "",
            "＃帮助": "",
            "/帮助": "",
            "#群管帮助": "群管",
            "＃群管 帮助": "群管 ",
        }
        for text, keyword in cases.items():
            with self.subTest(text=text):
                match = help_plugin.help_cmd.match(_event(text))
                self.assertIsNotNone(match)
                self.assertEqual(match.groups[0], keyword)

        for text in ("帮助", "前缀帮助", "#帮助啊", "群管帮助"):
            with self.subTest(text=text):
                self.assertIsNone(help_plugin.help_cmd.match(_event(text)))

    def test_matcher_options_match_legacy_contract(self) -> None:
        options = help_plugin.help_cmd.options
        self.assertEqual(options.name, "help")
        self.assertEqual(options.level, "all")
        self.assertEqual(options.scene, "all")
        self.assertEqual(options.priority, 5)
        self.assertTrue(options.block)


class HelpRendererTests(unittest.TestCase):
    def test_html_contains_title_groups_and_inlined_assets(self) -> None:
        from fxbot.plugins.help import renderer

        markup = renderer._build_html(
            "今汐帮助",
            "副标题",
            [{"group": "核心", "list": [{"icon": 1, "title": "#测试", "desc": "说明"}]}],
            3,
            renderer._default_background(),
            "footer",
        )
        self.assertIn("今汐帮助", markup)
        self.assertIn("#测试", markup)
        self.assertIn("核心", markup)
        self.assertIn("data:image/", markup)
        self.assertIn("data:font/", markup)

    def test_missing_playwright_returns_png_fallback(self) -> None:
        from fxbot.plugins.help import renderer

        async def render() -> bytes:
            with patch.dict(sys.modules, {"playwright": None, "playwright.async_api": None}):
                return await renderer.render_help_image("标题", "副标题", [])

        data = asyncio.run(render())
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))


class HelpHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_keyword_sends_nothing(self) -> None:
        bot = _Bot()
        await help_plugin._handle_help(_context(bot, "#绝不存在的帮助"))
        self.assertEqual(bot.sent, [])

    async def test_fallback_image_is_non_empty_png(self) -> None:
        data = help_plugin._fallback_image(
            "帮助",
            "测试",
            [{"group": "分组", "list": [{"title": "#命令", "desc": "说明"}]}],
        )
        self.assertGreater(len(data), 0)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_missing_renderer_sends_fallback_png(self) -> None:
        bot = _Bot()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(help_plugin, "cache_dir", return_value=Path(directory)),
                patch.object(help_plugin, "render_help_image", None),
            ):
                with self.assertRaises(_Finished):
                    await help_plugin._handle_help(_context(bot, "#帮助"))
        self.assertEqual(len(bot.sent), 1)
        segment = bot.sent[0][1].segments[0]
        self.assertEqual(segment.type, "image")
        self.assertTrue(segment.data["file"].startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_rendered_image_is_cached_outside_package(self) -> None:
        async def fake_render(**kwargs):
            return b"rendered-png"

        bot = _Bot()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(help_plugin, "cache_dir", return_value=root),
                patch.object(help_plugin, "render_help_image", fake_render),
            ):
                with self.assertRaises(_Finished):
                    await help_plugin._handle_help(_context(bot, "#帮助"))
            self.assertEqual((root / "help.png").read_bytes(), b"rendered-png")
        self.assertEqual(bot.sent[0][1].segments[0].data["file"], b"rendered-png")
        self.assertFalse((help_config.CFG_DIR / "help.png").exists())
