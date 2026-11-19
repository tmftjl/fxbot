"""QQ Webhook 的 Ed25519 校验与握手签名。"""

from __future__ import annotations

import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class SignatureRejected(ValueError):
    """无效或缺失的回调签名。"""


class QQSigner:
    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("QQ secret 不能为空")
        encoded = secret.encode("utf-8")
        seed = (encoded * ((32 + len(encoded) - 1) // len(encoded)))[:32]
        self._key = Ed25519PrivateKey.from_private_bytes(seed)

    def verify(self, body: bytes, timestamp: str, signature: str) -> None:
        if not timestamp:
            raise SignatureRejected("回调缺少时间戳")
        if not re.fullmatch(r"[0-9a-fA-F]{128}", signature):
            raise SignatureRejected("回调签名格式无效")
        try:
            value = bytes.fromhex(signature)
            if value[63] & 224:
                raise SignatureRejected("回调签名格式无效")
            self._key.public_key().verify(value, timestamp.encode("utf-8") + body)
        except InvalidSignature:
            raise SignatureRejected("回调签名不匹配") from None

    def challenge(self, data: object) -> dict[str, str]:
        if not isinstance(data, dict) or not isinstance(data.get("plain_token"), str):
            raise ValueError("回调握手缺少 plain_token")
        token, timestamp = data["plain_token"], data.get("event_ts")
        if not isinstance(timestamp, str):
            raise ValueError("回调握手数据无效")
        return {"plain_token": token, "signature": self._key.sign((timestamp + token).encode("utf-8")).hex()}
