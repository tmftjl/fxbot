"""独立监听配置与协议准入设置。"""

from __future__ import annotations

import json
import math
from types import MappingProxyType
from typing import Any
from pathlib import Path
from dataclasses import field, fields, dataclass
from collections.abc import Mapping

from ..platforms.qq.config import QQBotSettings


@dataclass(frozen=True, slots=True)
class Settings:
    """保存连接边界配置，令牌不参与文本表示。"""

    token: str = field(default="", repr=False)
    host: str = "127.0.0.1"
    port: int = 8683
    action_timeout: float = 15.0
    queue_size: int = 128
    event_concurrency: int = 8
    max_pending: int = 128
    close_timeout: float = 5.0
    qq_bots: Mapping[str, QQBotSettings] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """严格读取项目配置，令牌缺省时不鉴权，拒绝未知字段。"""
        from fxbot.utils.paths import config_dir

        source = path if path is not None else config_dir("runtime") / "config.json"
        return cls.from_mapping(json.loads(source.read_text(encoding="utf-8")))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Settings":
        """从内存对象构造设置，控制台保存和文件读取共用同一套校验。"""
        if not isinstance(raw, dict):
            raise ValueError("独立运行时配置必须是对象")
        if raw.keys() - {item.name for item in fields(cls)}:
            raise ValueError("独立运行时配置包含未知字段")
        values = dict(raw)
        if "qq_bots" in values:
            if not isinstance(values["qq_bots"], dict):
                raise ValueError("qq_bots 必须是 AppID 到凭证的对象")
            values["qq_bots"] = {app_id: QQBotSettings.parse(value) for app_id, value in values["qq_bots"].items()}
        return cls(**values)

    def to_payload(self) -> dict[str, Any]:
        """返回可写入配置文件并供控制台编辑的完整快照。"""
        return {
            "token": self.token,
            "host": self.host,
            "port": self.port,
            "action_timeout": self.action_timeout,
            "queue_size": self.queue_size,
            "event_concurrency": self.event_concurrency,
            "max_pending": self.max_pending,
            "close_timeout": self.close_timeout,
            "qq_bots": {app_id: bot.to_payload() for app_id, bot in self.qq_bots.items()},
        }

    def __post_init__(self) -> None:
        """令牌只允许空串或无首尾空白的文本，QQ 凭证独立校验。"""
        if not isinstance(self.token, str) or self.token != self.token.strip():
            raise ValueError("访问令牌必须是无首尾空白的字符串")
        if not isinstance(self.qq_bots, Mapping):
            raise ValueError("qq_bots 必须是 AppID 到凭证的映射")
        for app_id, credential in self.qq_bots.items():
            if (
                not isinstance(app_id, str)
                or not app_id.isascii()
                or not app_id.isdecimal()
                or int(app_id) <= 0
                or not isinstance(credential, QQBotSettings)
            ):
                raise ValueError("QQ AppID 必须为正十进制数字，并配置有效凭证")
        object.__setattr__(self, "qq_bots", MappingProxyType(dict(self.qq_bots)))
        self._validate_listen()

    def _validate_listen(self) -> None:
        """校验有限监听和容量参数。"""
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("监听地址不能为空")
        for value in (self.port, self.queue_size, self.event_concurrency, self.max_pending):
            if type(value) is not int or value <= 0:
                raise ValueError("端口和容量必须为正整数")
        if self.port > 65535:
            raise ValueError("端口超出范围")
        for timeout in (self.action_timeout, self.close_timeout):
            if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("动作和关闭超时必须为有限正数")


class SettingsState:
    """运行期可替换的设置快照；监听地址仍需重启进程才会生效。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def current(self) -> Settings:
        return self._settings

    def replace(self, settings: Settings) -> None:
        self._settings = settings
