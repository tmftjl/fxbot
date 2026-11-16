"""QQ Webhook 的 Ed25519 校验与握手签名，不允许关闭验签。"""

from __future__ import annotations

import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class SignatureRejected(ValueError):
    """无效、过期或缺失的回调签名。"""


class QQSigner:
    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("QQ secret 不能为空")
        encoded = secret.encode("utf-8")
        seed = (encoded * ((32 + len(encoded) - 1) // len(encoded)))[:32]
        self._key = Ed25519PrivateKey.from_private_bytes(seed)

    def verify(self, body: bytes, timestamp: str, signature: str) -> None:
        if not re.fullmatch(r"[0-9]{1,12}", timestamp) or abs(time.time() - int(timestamp)) > 300:
            raise SignatureRejected("回调时间戳无效或已过期")
        if not re.fullmatch(r"[0-9a-fA-F]{128}", signature):
            raise SignatureRejected("回调签名格式无效")
        try:
            self._key.public_key().verify(bytes.fromhex(signature), timestamp.encode("ascii") + body)
        except InvalidSignature:
            raise SignatureRejected("回调签名不匹配") from None

    def challenge(self, data: object) -> dict[str, str]:
        if not isinstance(data, dict) or not isinstance(data.get("plain_token"), str):
            raise ValueError("回调握手缺少 plain_token")
        token, timestamp = data["plain_token"], data.get("event_ts")
        if (
            not token
            or len(token) > 1024
            or not isinstance(timestamp, str)
            or not re.fullmatch(r"[0-9]{1,12}", timestamp)
        ):
            raise ValueError("回调握手数据无效")
        # 握手接口不能成为任意 Dispatch JSON 的签名器。
        if any(char in '{}[]"\\' or ord(char) < 32 for char in token):
            raise ValueError("握手 plain_token 必须是随机令牌，不能包含 JSON 结构")
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError("回调握手已过期")
        return {"plain_token": token, "signature": self._key.sign((timestamp + token).encode("utf-8")).hex()}
