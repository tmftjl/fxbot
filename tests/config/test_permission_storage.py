"""权限配置落盘只在内容真正变化时发生。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fxbot.permission import storage as permission_storage


class _Cache:
    """隔离进程全局缓存的测试替身。"""

    def __init__(self) -> None:
        self.values: dict = {}

    def remember(self, key, factory):
        if key not in self.values:
            self.values[key] = factory()
        return self.values[key]

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value) -> None:
        self.values[key] = value


class PermissionStorageFlushTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(permission_storage, "config_dir", return_value=Path(directory.name)))
        self.enterContext(patch.object(permission_storage, "cache", _Cache()))
        self.storage = permission_storage.PermissionStorage()
        self.storage.ensure()
        self.data = self.storage.load()

    def test_unchanged_dirty_flush_skips_disk_write(self) -> None:
        self.storage.mark_dirty(self.data)
        with patch.object(permission_storage.ConfigStorage, "write") as write:
            self.storage.flush()
        write.assert_not_called()
        self.assertFalse(self.storage._dirty)

    def test_changed_flush_writes_once_then_stops(self) -> None:
        self.data["top"]["blacklist"]["users"].append("777")
        self.storage.mark_dirty(self.data)
        with patch.object(permission_storage.ConfigStorage, "write") as write:
            self.storage.flush()
        write.assert_called_once()
        self.storage.mark_dirty(self.data)
        with patch.object(permission_storage.ConfigStorage, "write") as write:
            self.storage.flush()
        write.assert_not_called()

    def test_failed_save_keeps_dirty_and_snapshot_for_retry(self) -> None:
        self.data["top"]["blacklist"]["users"].append("777")
        self.storage.mark_dirty(self.data)
        persisted = self.storage._persisted
        with patch.object(
            permission_storage.ConfigStorage, "write", side_effect=PermissionError("locked")
        ):
            with self.assertRaises(PermissionError):
                self.storage.flush()
        self.assertTrue(self.storage._dirty)
        self.assertEqual(self.storage._persisted, persisted)
        with patch.object(permission_storage.ConfigStorage, "write") as write:
            self.storage.flush()
        write.assert_called_once()
        self.assertFalse(self.storage._dirty)
        self.assertNotEqual(self.storage._persisted, persisted)


if __name__ == "__main__":
    unittest.main()
