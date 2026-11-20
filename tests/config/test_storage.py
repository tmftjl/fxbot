"""配置文件原子写入在 Windows 文件占用下的重试边界。"""

from __future__ import annotations

import os
import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fxbot.config import storage as config_storage


class AtomicWriteRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.target = self.directory / "config.json"
        self.storage = config_storage.ConfigStorage("test", path=self.target)

    def test_transient_permission_error_is_retried_until_success(self) -> None:
        calls: list[int] = []

        def flaky(source, destination):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("locked")
            os.replace(source, destination)

        sleep = Mock()
        with (
            patch.object(config_storage, "os", SimpleNamespace(replace=flaky)),
            patch.object(config_storage, "sys", SimpleNamespace(platform="win32")),
            patch.object(config_storage, "time", SimpleNamespace(sleep=sleep)),
        ):
            self.storage.write({"answer": 42})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"answer": 42})

    def test_persistent_permission_error_is_raised_and_temp_file_removed(self) -> None:
        replace = Mock(side_effect=PermissionError("locked"))
        sleep = Mock()
        with (
            patch.object(config_storage, "os", SimpleNamespace(replace=replace)),
            patch.object(config_storage, "sys", SimpleNamespace(platform="win32")),
            patch.object(config_storage, "time", SimpleNamespace(sleep=sleep)),
        ):
            with self.assertRaises(PermissionError):
                self.storage.write({"answer": 42})
        self.assertEqual(replace.call_count, config_storage._REPLACE_ATTEMPTS)
        self.assertEqual(sleep.call_count, config_storage._REPLACE_ATTEMPTS - 1)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_failed_replace_keeps_existing_file_intact(self) -> None:
        self.target.write_text(json.dumps({"old": True}), encoding="utf-8")
        replace = Mock(side_effect=PermissionError("locked"))
        with (
            patch.object(config_storage, "os", SimpleNamespace(replace=replace)),
            patch.object(config_storage, "sys", SimpleNamespace(platform="win32")),
            patch.object(config_storage, "time", SimpleNamespace(sleep=Mock())),
        ):
            with self.assertRaises(PermissionError):
                self.storage.write({"answer": 42})
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"old": True})

    def test_non_windows_permission_error_is_not_retried(self) -> None:
        replace = Mock(side_effect=PermissionError("denied"))
        sleep = Mock()
        with (
            patch.object(config_storage, "os", SimpleNamespace(replace=replace)),
            patch.object(config_storage, "sys", SimpleNamespace(platform="linux")),
            patch.object(config_storage, "time", SimpleNamespace(sleep=sleep)),
        ):
            with self.assertRaises(PermissionError):
                self.storage.write({"answer": 42})
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
