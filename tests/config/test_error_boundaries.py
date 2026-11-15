"""只对配置输入错误回退，内部编程错误必须向调用方暴露。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fxbot.utils.cache import cache
from fxbot.config.proxy import ConfigProxy
from fxbot.config.errors import ConfigFormatError, ConfigValidationError


class ConfigErrorBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.proxy = ConfigProxy("boundary-test", {"enabled": True}, validator=self.validate)
        self.proxy.storage._path = self.path
        self.addCleanup(cache.pop, self.proxy._cache_key)
        self.proxy.save({"enabled": True})

    @staticmethod
    def validate(raw):
        if type(raw.get("enabled", True)) is not bool:
            raise ValueError("enabled 必须为布尔值")

    def test_internal_validator_errors_are_not_history_fallback(self):
        self.path.write_text('{"enabled": false}', encoding="utf-8")
        for error in (RuntimeError("bug"), TypeError("bug"), AttributeError("bug"), AssertionError("bug")):
            with self.subTest(error=type(error).__name__), patch.object(self.proxy, "validator", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    self.proxy.reload()
                self.assertIs(caught.exception, error)
                self.assertEqual(self.proxy._last_valid, {"enabled": True})
                self.assertEqual(self.proxy.history_storage.read(strict=True), {"enabled": True})
                self.assertEqual(self.path.read_text(encoding="utf-8"), '{"enabled": false}')

    def test_internal_storage_error_is_not_replaced_with_old_config(self):
        with patch.object(self.proxy.storage, "read", side_effect=RuntimeError("storage-bug")):
            with self.assertRaisesRegex(RuntimeError, "storage-bug"):
                self.proxy.reload()

    def test_declared_validation_error_keeps_last_valid(self):
        raw = '{"enabled": "wrong"}'
        self.path.write_text(raw, encoding="utf-8")
        self.assertEqual(self.proxy.reload(), {"enabled": True})
        self.assertEqual(self.path.read_text(encoding="utf-8"), raw)
        with self.assertRaises(ConfigValidationError):
            self.proxy.save({"enabled": "wrong"})

    def test_invalid_utf8_is_input_error_and_preserves_original_bytes(self):
        self.path.write_bytes(b"\xff\xfeinvalid")
        self.assertEqual(self.proxy.reload(), {"enabled": True})
        self.assertEqual(self.path.read_bytes(), b"\xff\xfeinvalid")
        with self.assertRaises(ConfigFormatError):
            self.proxy.storage.read(strict=True)

    def test_bootstrap_merge_does_not_swallow_internal_validator_error(self):
        with patch.object(self.proxy, "validator", side_effect=RuntimeError("merge-bug")):
            with self.assertRaisesRegex(RuntimeError, "merge-bug"):
                self.proxy.merge_and_save()

    def test_corrupt_primary_with_buggy_history_validation_exposes_bug(self):
        self.path.write_text("{invalid", encoding="utf-8")
        self.proxy._last_valid = None
        with patch.object(self.proxy, "validator", side_effect=RuntimeError("history-bug")):
            with self.assertRaisesRegex(RuntimeError, "history-bug"):
                self.proxy.reload()
