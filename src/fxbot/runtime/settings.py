"""独立监听配置与协议准入设置。"""

from __future__ import annotations

import json
import math
from types import MappingProxyType
from pathlib import Path
from dataclasses import field, fields, dataclass
from collections.abc import Mapping

from ..platforms.qq.config import QQBotSettings


@dataclass(frozen=True, slots=True)
class Settings:
    """保存连接边界配置，令牌不参与文本表示。"""

    token: str = field(repr=False)
    bots: Mapping[str, frozenset[str]]
    host: str = "127.0.0.1"
    port: int = 8683
    action_timeout: float = 15.0
    queue_size: int = 128
    max_pending: int = 128
    close_timeout: float = 5.0
    onebot11_implementations: Mapping[str, str] = field(default_factory=dict)
    qq_bots: Mapping[str, QQBotSettings] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """严格读取项目配置，拒绝缺失身份、未知字段和错误结构。"""
        from fxbot.utils.paths import config_dir

        source = path if path is not None else config_dir("runtime") / "config.json"
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("独立运行时配置必须是对象")
        if not {"token", "bots"} <= raw.keys():
            raise ValueError("必须配置 token 和 bots")
        if raw.keys() - {item.name for item in fields(cls)}:
            raise ValueError("独立运行时配置包含未知字段")
        if not isinstance(raw["bots"], dict) or any(
            not isinstance(values, list) or not all(isinstance(value, str) for value in values)
            for values in raw["bots"].values()
        ):
            raise ValueError("bots 必须按协议平台列出身份字符串数组")
        if "qq_bots" in raw:
            if not isinstance(raw["qq_bots"], dict):
                raise ValueError("qq_bots 必须是 AppID 到凭证的对象")
            raw["qq_bots"] = {app_id: QQBotSettings.parse(value) for app_id, value in raw["qq_bots"].items()}
        return cls(**{**raw, "bots": {key: frozenset(values) for key, values in raw["bots"].items()}})

    def __post_init__(self) -> None:
        """拒绝空鉴权、未知身份和无界资源设置。"""
        if not isinstance(self.token, str) or not self.token.strip() or self.token != self.token.strip():
            raise ValueError("必须配置非空访问令牌")
        if not isinstance(self.bots, Mapping) or not self.bots:
            raise ValueError("必须配置按平台隔离的 Bot 白名单")
        for platform, identities in self.bots.items():
            if not isinstance(platform, str) or not platform or not isinstance(identities, frozenset) or not identities:
                raise ValueError("平台和 Bot 白名单必须明确且非空")
            if any(not isinstance(identity, str) or not identity.strip() for identity in identities):
                raise ValueError("Bot 身份必须为非空字符串")
        object.__setattr__(self, "bots", MappingProxyType(dict(self.bots)))
        if not isinstance(self.onebot11_implementations, Mapping):
            raise ValueError("onebot11_implementations 必须是 Bot ID 到实现类型的映射")
        for bot_id, implementation in self.onebot11_implementations.items():
            if (
                not isinstance(bot_id, str)
                or not isinstance(implementation, str)
                or bot_id not in self.bots.get("onebot11", ())
                or implementation not in {"standard", "napcat"}
            ):
                raise ValueError("V11 实现必须明确为 standard 或 napcat，且对应已配置的 Bot")
        object.__setattr__(self, "onebot11_implementations", MappingProxyType(dict(self.onebot11_implementations)))
        if not isinstance(self.qq_bots, Mapping):
            raise ValueError("qq_bots 必须是 AppID 到凭证的映射")
        if set(self.qq_bots) != set(self.bots.get("qq", ())):
            raise ValueError("qq_bots 必须与 bots.qq 白名单一一对应")
        for app_id, credential in self.qq_bots.items():
            if (
                not app_id.isascii()
                or not app_id.isdecimal()
                or int(app_id) <= 0
                or not isinstance(credential, QQBotSettings)
            ):
                raise ValueError("QQ AppID 必须为正十进制数字，并配置有效凭证")
        object.__setattr__(self, "qq_bots", MappingProxyType(dict(self.qq_bots)))
        self._validate_listen()

    def allows(self, platform: str, bot_id: str) -> bool:
        """连接准入同时匹配平台与机器人身份。"""
        return bot_id in self.bots.get(platform, ())

    def _validate_listen(self) -> None:
        """校验有限监听和容量参数。"""
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("监听地址不能为空")
        for value in (self.port, self.queue_size, self.max_pending):
            if type(value) is not int or value <= 0:
                raise ValueError("端口和容量必须为正整数")
        if self.port > 65535:
            raise ValueError("端口超出范围")
        for value in (self.action_timeout, self.close_timeout):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("动作和关闭超时必须为有限正数")
