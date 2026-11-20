"""运行时 JSON 权限配置存储。"""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path

from .types import perm_entry_default
from .validation import validate_permissions
from ..utils.cache import cache
from ..utils.paths import config_dir
from ..config.storage import ConfigStorage


def _fingerprint(data: dict[str, Any]) -> str:
    """只用于判重，排序后紧凑序列化，不受缩进和插入顺序影响。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PermissionStorage:
    """权限配置文件存储。"""

    def __init__(self) -> None:
        self._path = config_dir() / "permissions.json"
        self._cache_key = ("permissions",)
        self._dirty = False
        self._persisted: str | None = None

    @property
    def path(self) -> Path:
        """权限配置文件路径。"""
        return self._path

    def ensure(self) -> None:
        """确保权限配置文件存在。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text(
                json.dumps({"top": perm_entry_default(), "sub_plugins": {}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _read(self) -> dict[str, Any]:
        """从磁盘读取权限配置。"""
        data = json.loads(self._path.read_text(encoding="utf-8"))
        validate_permissions(data)
        self._persisted = _fingerprint(data)
        return data

    def load(self) -> dict[str, Any]:
        """加载权限配置，按进程内缓存复用。"""
        self.ensure()
        return cache.remember(self._cache_key, self._read)

    def reload(self) -> None:
        """从磁盘重新加载权限配置。"""
        cache.set(self._cache_key, self._read())
        self._dirty = False

    def save(self, data: dict[str, Any]) -> None:
        """保存权限配置并同步缓存。"""
        validate_permissions(data)
        storage = ConfigStorage("permissions", path=self._path)
        storage.write(data)
        cache.set(self._cache_key, data)
        self._dirty = False
        self._persisted = _fingerprint(data)

    def mark_dirty(self, data: dict[str, Any]) -> None:
        """标记权限配置为待写入。"""
        validate_permissions(data)
        cache.set(self._cache_key, data)
        self._dirty = True

    def flush(self) -> None:
        """写入待保存的权限配置。

        指纹只与上次成功读取或写入的快照比较，用于跳过本进程内无意义的重复写入；
        不检测外部对文件的实时修改，也不提供并发写入保护。
        """
        if self._dirty:
            data = cache.get(self._cache_key)
            if data is not None:
                if _fingerprint(data) == self._persisted:
                    self._dirty = False
                    return
                self.save(data)


_global_storage = PermissionStorage()


def get_storage() -> PermissionStorage:
    """获取全局权限存储。"""
    return _global_storage
