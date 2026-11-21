"""今日运势卡的抽签、宜忌与 HTML 构建边界。"""

from __future__ import annotations

import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from fxbot.plugins.entertain import fortune

RECORD = {
    "time": "2026-11-20",
    "fortune": {
        "fortuneSummary": "大吉",
        "luckyStar": "★★★★★★☆",
        "signText": "草木逢春，枯叶沾露",
        "unsignText": "今天适合把想了很久的事情动手做起来。",
    },
}


class FortuneCardTests(unittest.TestCase):
    def test_weighted_pick_keeps_extreme_stars_rare(self) -> None:
        entries = []
        for tier in range(8):
            entries.extend(
                {"fortuneSummary": f"tier{tier}", "luckyStar": "★" * tier + "☆" * (7 - tier)}
                for _ in range(80)
            )
        rng = random.Random(7)
        counts = {tier: 0 for tier in range(8)}
        for _ in range(5000):
            item = fortune._pick_fortune(entries, rng)
            counts[fortune._star_count(item)] += 1
        self.assertLess(counts[0] / 5000, 0.10)
        self.assertGreater((counts[3] + counts[4] + counts[5]) / 5000, 0.45)
        self.assertLess((counts[6] + counts[7]) / 5000, 0.30)

    def test_seed_follows_user_date_and_recast(self) -> None:
        base = fortune._fortune_seed("200", {"time": "2026-11-20"})
        self.assertEqual(base, "200:2026-11-20:0")
        self.assertNotEqual(base, fortune._fortune_seed("201", {"time": "2026-11-20"}))
        self.assertNotEqual(base, fortune._fortune_seed("200", {"time": "2026-11-21"}))
        self.assertNotEqual(base, fortune._fortune_seed("200", {"time": "2026-11-20", "recast": 1}))

    def test_recast_rerolls_once_per_day(self) -> None:
        entries = [
            {"fortuneSummary": "超好运", "luckyStar": "★★★★★★★", "signText": "甲"},
            {"fortuneSummary": "极背", "luckyStar": "☆☆☆☆☆☆☆", "signText": "乙"},
        ]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(fortune, "USER_DATA_FILE", Path(tmp) / "user_fortunes.json"),
            patch.object(fortune, "_USER_FORTUNES", {}),
            patch.object(fortune, "_JRYS_DATA", entries),
        ):
            record, _ = fortune._get_or_create_today_fortune("200")
            first = record["fortune"]
            record, changed = fortune._recast_today_fortune("200")
            self.assertTrue(changed)
            self.assertNotEqual(record["fortune"], first)
            self.assertEqual(record["recast"], 1)
            _, changed_again = fortune._recast_today_fortune("200")
            self.assertFalse(changed_again)

    def test_good_and_bad_activities_never_share_a_group(self) -> None:
        groups = {name: group for name, group in fortune._GOOD + fortune._BAD}
        counts = {2: 0, 3: 0}
        for index in range(300):
            good, bad = fortune._activities(f"user-{index}")
            self.assertIn(len(good), (2, 3))
            self.assertIn(len(bad), (2, 3))
            self.assertEqual(len(set(good)), len(good))
            self.assertEqual(len(set(bad)), len(bad))
            self.assertFalse({groups[name] for name in good} & {groups[name] for name in bad})
            counts[len(good)] += 1
            counts[len(bad)] += 1
        self.assertTrue(all(counts.values()), counts)

    def test_card_html_fills_tokens_and_escapes_user_text(self) -> None:
        record = {
            "time": "2026-11-20",
            "fortune": {**RECORD["fortune"], "signText": "<script>alert(1)</script>"},
        }
        markup = fortune.build_card_html("200", record, "<群友>")
        self.assertNotIn("{{", markup)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", markup)
        self.assertIn("&lt;群友&gt;", markup)
        self.assertIn("大吉", markup)
        self.assertIn("data:image/png;base64,", markup)
        self.assertIn("<svg", markup)

    def test_bundled_fonts_are_embedded(self) -> None:
        markup = fortune.build_card_html("200", RECORD, "群友")
        self.assertIn("@font-face", markup)
        self.assertEqual(markup.count("data:font/woff2;base64,"), 2)
        self.assertIn("'FortuneSans'", markup)
        self.assertIn("'FortuneKai'", markup)

    def test_real_avatar_bytes_override_initial_avatar(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), (10, 20, 30)).save(buffer, format="JPEG")
        markup = fortune.build_card_html("200", RECORD, "群友", avatar=buffer.getvalue())
        self.assertIn("data:image/jpeg;base64,", markup)

    def test_default_background_is_embedded_when_api_returns_nothing(self) -> None:
        captured: dict[str, object] = {}

        def fake_html(user_id, record, nickname, background=None, avatar=None):
            captured["background"] = background
            return "<html></html>"

        with (
            patch.object(fortune, "_fetch_background_bytes", AsyncMock(return_value=None)),
            patch.object(fortune, "build_card_html", fake_html),
            patch("fxbot.runtime.browser.render_html", AsyncMock(return_value=b"\x89PNG")),
        ):
            __import__("asyncio").run(fortune.render_card_bytes("200", RECORD, "群友"))
        background = captured["background"]
        self.assertIsInstance(background, bytes)
        with Image.open(io.BytesIO(background)) as probe:
            self.assertEqual(probe.format, "JPEG")

    def test_render_falls_back_to_pil_when_browser_fails(self) -> None:
        with (
            patch.object(fortune, "_fetch_background_bytes", AsyncMock(return_value=None)),
            patch("fxbot.runtime.browser.render_html", AsyncMock(side_effect=RuntimeError("offline"))),
        ):
            image = __import__("asyncio").run(
                fortune.render_card_bytes("200", RECORD, "群友")
            )
        self.assertTrue(image.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
