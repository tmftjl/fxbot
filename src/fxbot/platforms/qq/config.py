"""只读取项目配置的 QQ 官方凭证。"""

from __future__ import annotations

import re
from dataclasses import field, dataclass


@dataclass(frozen=True, slots=True)
class QQBotSettings:
    secret: str = field(repr=False)
    bot_user_id: str = ""
    mode: str = "webhook"
    intents: int = (1 << 25) | (1 << 30)
    shard: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.secret, str) or not self.secret or self.secret != self.secret.strip():
            raise ValueError("QQ secret 必须为非空字符串")
        if not isinstance(self.bot_user_id, str) or self.bot_user_id != self.bot_user_id.strip():
            raise ValueError("QQ bot_user_id 必须是用户 ID 字符串")
        if self.bot_user_id and not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", self.bot_user_id):
            raise ValueError("QQ bot_user_id 格式无效")
        if not isinstance(self.mode, str) or self.mode not in {"webhook", "websocket"}:
            raise ValueError("QQ mode 必须为 webhook 或 websocket")
        if type(self.intents) is not int or not 0 < self.intents < (1 << 31):
            raise ValueError("QQ intents 必须为非零的 31 位权限位掩码")
        if self.shard is not None and (
            not isinstance(self.shard, tuple)
            or len(self.shard) != 2
            or any(type(value) is not int for value in self.shard)
            or not 0 <= self.shard[0] < self.shard[1]
        ):
            raise ValueError("QQ shard 必须为 [分片序号, 分片总数]")
        if self.mode == "webhook" and self.shard is not None:
            raise ValueError("Webhook 不配置分片")

    @classmethod
    def parse(cls, raw: dict) -> QQBotSettings:
        if (
            not isinstance(raw, dict)
            or not {"secret"} <= raw.keys()
            or raw.keys() - {"secret", "bot_user_id", "mode", "intents", "shard"}
        ):
            raise ValueError("QQ Bot 配置存在缺失或未知字段")
        raw = dict(raw)
        if "shard" in raw and raw["shard"] is not None:
            if not isinstance(raw["shard"], list):
                raise ValueError("QQ shard 必须为数组")
            raw["shard"] = tuple(raw["shard"])
        return cls(**raw)
