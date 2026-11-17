"""字体回退绘制基线对齐回归。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from fxbot.utils.fonts import draw_text_with_fallback, load_fallback_font_pair


class FontFallbackTests(unittest.TestCase):
    def test_fallback_segments_share_primary_baseline(self) -> None:
        primary, fallback = load_fallback_font_pair(size=50)
        draw = Mock()
        draw_text_with_fallback(draw, (5, 10), "今汐", primary, fallback, fill="white")
        baseline = 10 + primary.getmetrics()[0]
        calls = draw.text.call_args_list
        self.assertGreaterEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call.args[0][1], baseline)
            self.assertEqual(call.kwargs["anchor"], "ls")


if __name__ == "__main__":
    unittest.main()
