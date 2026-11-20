"""运势库数据完整性：64 卦 × 6 爻、等级与星级一致、文案不重复。"""

from __future__ import annotations

import json
import unittest

from fxbot.plugins.entertain import fortune

LEVELS = ["超好运", "好运", "小顺", "平平", "小坎", "不顺", "背", "极背"]
TRIGRAMS = {"乾", "坤", "震", "巽", "坎", "离", "艮", "兑"}
FIELDS = {"id", "hexagram", "symbol", "upper", "lower", "line", "original", "plain", "level", "stars"}


class FortuneDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.entries = json.loads(fortune.JRYS_DEFS_FILE.read_text(encoding="utf-8"))

    def test_every_hexagram_has_six_lines(self) -> None:
        self.assertEqual(len(self.entries), 384)
        counter: dict[str, int] = {}
        for item in self.entries:
            counter[item["hexagram"]] = counter.get(item["hexagram"], 0) + 1
        self.assertEqual(len(counter), 64)
        self.assertEqual(set(counter.values()), {6})

    def test_required_fields_present(self) -> None:
        for item in self.entries:
            self.assertTrue(FIELDS.issubset(item), item.get("id"))
            self.assertIn(item["upper"], TRIGRAMS)
            self.assertIn(item["lower"], TRIGRAMS)
            self.assertIn(item["line"][0], "初六九上")

    def test_level_matches_stars(self) -> None:
        for item in self.entries:
            self.assertIn(item["level"], LEVELS, item["id"])
            self.assertEqual(item["stars"], 7 - LEVELS.index(item["level"]), item["id"])
        self.assertEqual({item["level"] for item in self.entries}, set(LEVELS))

    def test_ids_and_texts_are_unique(self) -> None:
        ids = [item["id"] for item in self.entries]
        originals = [item["original"] for item in self.entries]
        plains = [item["plain"] for item in self.entries]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(len(set(originals)), len(originals))
        self.assertEqual(len(set(plains)), len(plains))
        self.assertTrue(all(plain.strip() for plain in plains))


if __name__ == "__main__":
    unittest.main()
