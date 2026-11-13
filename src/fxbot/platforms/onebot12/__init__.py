"""OneBot V12 的原始协议和客户端。"""

from __future__ import annotations

from .codec import parse_event, parse_message, encode_message
from .client import OneBot12Client

__all__ = ["OneBot12Client", "parse_event", "parse_message", "encode_message"]
