"""表情元数据、群组开关与关键词索引管理。"""

from __future__ import annotations

import asyncio
from enum import IntEnum
from typing import Any, Optional
from pathlib import Path
from dataclasses import field, dataclass

import yaml
from loguru import logger
from rapidfuzz import process

from .config import cfg_disabled_list
from .request import MemeInfo, get_meme_info, get_meme_keys
from ...utils.paths import data_dir


def config_path() -> Path:
    """表情开关文件路径；延迟到使用时建立目录。"""
    return data_dir("memes") / "memes_config.yaml"


class MemeMode(IntEnum):
    BLACK = 0
    WHITE = 1


@dataclass(slots=True)
class MemeConfig:
    mode: MemeMode = MemeMode.BLACK
    disabled_groups: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {"mode": int(self.mode), "disabled_groups": list(self.disabled_groups)}

    @classmethod
    def from_record(cls, raw: Any) -> "MemeConfig":
        if not isinstance(raw, dict):
            raise ValueError("表情配置必须是对象")
        mode = raw.get("mode", 0)
        if isinstance(mode, bool) or mode not in (0, 1, "0", "1"):
            raise ValueError("表情配置模式无效")
        groups = raw.get("disabled_groups", [])
        if not isinstance(groups, list) or any(group is None for group in groups):
            raise ValueError("表情配置禁用列表无效")
        return cls(MemeMode(int(mode)), [str(group) for group in groups])


class MemeManager:
    """表情列表与开关文件；初始化失败不永久锁定，后续请求可以重试。"""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._ready = False
        self._initialization_requested = False
        self._lock = asyncio.Lock()
        self._revision = 0
        self._meme_config: dict[str, MemeConfig] = {}
        self._meme_dict: dict[str, MemeInfo] = {}
        self._meme_names: dict[str, list[MemeInfo]] = {}
        self._meme_tags: dict[str, list[MemeInfo]] = {}

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else config_path()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def revision(self) -> int:
        return self._revision

    async def init(self) -> None:
        """从表情服务加载全部表情，并恢复/补全开关文件。"""
        disabled_list = set(cfg_disabled_list())
        keys = sorted(await get_meme_keys())
        memes = {
            meme_key: await get_meme_info(meme_key)
            for meme_key in filter(lambda meme_key: meme_key not in disabled_list, keys)
        }
        self._meme_dict = memes
        self._load()
        self._dump()
        self._refresh_names()
        self._refresh_tags()
        self._ready = True
        self._revision += 1

    async def ensure_ready(self) -> bool:
        """首次使用时初始化；服务不可用时保留空列表并记录日志。"""
        if self._ready:
            return True
        async with self._lock:
            if self._ready:
                return True
            try:
                await self.init()
            except Exception:
                logger.opt(exception=True).warning("[memes] 表情列表初始化失败")
                return False
        return True

    def request_initialization(self) -> None:
        """匹配路径只登记元数据需求，不等待网络或初始化锁。"""
        if not self._ready:
            self._initialization_requested = True

    async def initialize_requested(self) -> None:
        """周期业务消费需求；失败不在每条消息上同步重试。"""
        if self._initialization_requested:
            self._initialization_requested = False
            await self.ensure_ready()

    def get_meme(self, meme_key: str) -> Optional[MemeInfo]:
        return self._meme_dict.get(meme_key, None)

    def get_memes(self) -> list[MemeInfo]:
        return list(self._meme_dict.values())

    def block(self, user_id: str, meme_key: str) -> bool:
        """禁用表情（群组级别）。"""
        config = self._meme_config[meme_key]
        if config.mode == MemeMode.WHITE:
            return False
        if user_id not in config.disabled_groups:
            config.disabled_groups.append(user_id)
            self._dump()
            return True
        return False

    def unblock(self, user_id: str, meme_key: str) -> bool:
        """启用表情（群组级别）。"""
        config = self._meme_config[meme_key]
        if config.mode == MemeMode.WHITE:
            return False
        if user_id in config.disabled_groups:
            config.disabled_groups.remove(user_id)
            self._dump()
            return True
        return False

    def get_black_list(self) -> list[str]:
        """获取全局禁用的表情列表。"""
        return [key for key, config in self._meme_config.items() if config.mode == MemeMode.WHITE]

    def change_mode(self, mode: MemeMode, meme_key: str) -> None:
        config = self._meme_config[meme_key]
        config.mode = mode
        self._dump()

    def find(self, meme_name: str) -> list[MemeInfo]:
        return self._meme_names.get(meme_name.lower(), [])

    def search(
        self,
        meme_name: str,
        include_tags: bool = False,
        limit: Optional[int] = None,
        score_cutoff: float = 80.0,
    ) -> list[MemeInfo]:
        meme_name = meme_name.lower()
        meme_names = process.extract(meme_name, self._meme_names.keys(), limit=limit, score_cutoff=score_cutoff)
        result: dict[str, MemeInfo] = {}
        for name, _, _ in meme_names:
            for meme in self._meme_names[name]:
                result[meme.key] = meme
        if include_tags:
            meme_tags = process.extract(meme_name, self._meme_tags.keys(), limit=limit, score_cutoff=score_cutoff)
            for tag, _, _ in meme_tags:
                for meme in self._meme_tags[tag]:
                    result[meme.key] = meme
        return list(result.values())

    def check(self, user_id: str, meme_key: str) -> bool:
        """检查表情是否可用。"""
        config = self._meme_config.get(meme_key)
        if config is None:
            return False
        if config.mode == MemeMode.WHITE:
            return False
        if user_id in config.disabled_groups:
            return False
        return True

    def _load(self) -> None:
        raw_list: dict[str, Any] = {}
        path = self.path
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as file:
                    raw = yaml.safe_load(file)
                if isinstance(raw, dict):
                    raw_list = raw
                else:
                    logger.warning("表情列表解析失败，将重新生成")
            except Exception:
                logger.warning("表情列表解析失败，将重新生成")
        try:
            meme_list = {str(name): MemeConfig.from_record(config) for name, config in raw_list.items()}
        except Exception:
            meme_list = {}
            logger.warning("表情列表解析失败，将重新生成")
        self._meme_config = {meme_key: MemeConfig() for meme_key in self._meme_dict}
        self._meme_config.update(meme_list)

    def _dump(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        meme_list = {name: config.to_record() for name, config in self._meme_config.items()}
        with path.open("w", encoding="utf-8") as file:
            yaml.dump(meme_list, file, allow_unicode=True)

    def _refresh_names(self) -> None:
        self._meme_names = {}
        for meme in self._meme_dict.values():
            names = {meme.key.lower()}
            names.update(keyword.lower() for keyword in meme.keywords)
            names.update((shortcut.humanized or shortcut.key).lower() for shortcut in meme.shortcuts)
            for name in names:
                self._meme_names.setdefault(name, []).append(meme)

    def _refresh_tags(self) -> None:
        self._meme_tags = {}
        for meme in self._meme_dict.values():
            for raw_tag in meme.tags:
                tag = raw_tag.lower()
                self._meme_tags.setdefault(tag, []).append(meme)


meme_manager = MemeManager()
