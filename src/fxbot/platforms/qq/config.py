"""只读取项目配置的 QQ 官方凭证。"""

from __future__ import annotations

from dataclasses import field, dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class QQBotSettings:
    secret: str = field(repr=False)
    bot_user_id: str = ""
    mode: str = "websocket"
    intents: int = (1 << 24) | (1 << 25) | (1 << 26) | (1 << 27)
    shard: tuple[int, int] | None = None
    api_base: str = "https://api.bot.qq.com"
    auth_url: str = "https://api.bot.qq.com/app/getAppAccessToken"
    gateway_url: str | None = None
    verify_webhook: bool = True

    def __post_init__(self) -> None:
        for value, schemes in (
            (self.api_base, {"http", "https"}),
            (self.auth_url, {"http", "https"}),
            (self.gateway_url, {"ws", "wss"}),
        ):
            if value is None and "wss" in schemes:
                continue
            if not isinstance(value, str):
                raise ValueError("QQ 接口地址必须为字符串")
            parsed = urlsplit(value)
            if parsed.scheme not in schemes or not parsed.hostname:
                raise ValueError("QQ 接口地址必须包含协议和主机")
        if not isinstance(self.secret, str) or not self.secret:
            raise ValueError("QQ secret 必须为非空字符串")
        if not isinstance(self.bot_user_id, str):
            raise ValueError("QQ bot_user_id 必须是用户 ID 字符串")
        if not isinstance(self.mode, str) or self.mode not in {"webhook", "websocket"}:
            raise ValueError("QQ mode 必须为 webhook 或 websocket")
        if type(self.intents) is not int:
            raise ValueError("QQ intents 必须为整数")
        if type(self.verify_webhook) is not bool:
            raise ValueError("QQ verify_webhook 必须为布尔值")
        if self.shard is not None and (
            not isinstance(self.shard, tuple)
            or len(self.shard) != 2
            or any(type(value) is not int for value in self.shard)
        ):
            raise ValueError("QQ shard 必须为 [分片序号, 分片总数]")

    @classmethod
    def parse(cls, raw: dict) -> QQBotSettings:
        if not isinstance(raw, dict) or not {"secret"} <= raw.keys():
            raise ValueError("QQ Bot 配置存在缺失或未知字段")
        raw = {key: value for key, value in raw.items() if key in cls.__dataclass_fields__}
        if "shard" in raw and raw["shard"] is not None:
            if not isinstance(raw["shard"], list):
                raise ValueError("QQ shard 必须为数组")
            raw["shard"] = tuple(raw["shard"])
        return cls(**raw)

    def to_payload(self) -> dict:
        """返回可写回配置文件并供控制台编辑的凭证快照。"""
        payload = {
            "secret": self.secret,
            "mode": self.mode,
            "bot_user_id": self.bot_user_id,
            "intents": self.intents,
            "api_base": self.api_base,
            "auth_url": self.auth_url,
            "verify_webhook": self.verify_webhook,
        }
        if self.shard is not None:
            payload["shard"] = list(self.shard)
        if self.gateway_url is not None:
            payload["gateway_url"] = self.gateway_url
        return payload
