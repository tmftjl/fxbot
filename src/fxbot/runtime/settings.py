"""独立监听配置与协议准入设置。"""

from __future__ import annotations

import math
from dataclasses import field, dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """保存连接边界配置，令牌不参与文本表示。"""

    token: str = field(repr=False)
    bot_ids: frozenset[str]
    host: str = "127.0.0.1"
    port: int = 8080
    action_timeout: float = 15.0
    queue_size: int = 128
    max_pending: int = 128

    def __post_init__(self) -> None:
        """拒绝空鉴权、未知身份和无界资源设置。"""
        if not isinstance(self.token, str) or not self.token.strip() or self.token != self.token.strip():
            raise ValueError("必须配置非空访问令牌")
        if not self.bot_ids or any(
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or str(int(value)) != value
            or int(value) <= 0
            for value in self.bot_ids
        ):
            raise ValueError("必须配置规范的 Bot ID 白名单")
        object.__setattr__(self, "bot_ids", frozenset(self.bot_ids))
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("监听地址不能为空")
        for value in (self.port, self.queue_size, self.max_pending):
            if type(value) is not int or value <= 0:
                raise ValueError("端口和容量必须为正整数")
        if self.port > 65535:
            raise ValueError("端口超出范围")
        if isinstance(self.action_timeout, bool) or not math.isfinite(self.action_timeout) or self.action_timeout <= 0:
            raise ValueError("动作超时必须为有限正数")
