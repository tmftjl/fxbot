"""控制台账户：单账号密码登录与首次登录改密标记。"""

from __future__ import annotations

import hmac
import json
import string
import hashlib
import secrets
from enum import Enum
from typing import Any
from pathlib import Path

from loguru import logger

from ..utils.paths import config_dir

USERNAME = "root"
MIN_PASSWORD = 8
_ITERATIONS = 200_000
_ALPHABET = string.ascii_letters + string.digits


class PasswordRejection(Enum):
    """客户可见的固定密码操作拒绝原因。"""

    WRONG_PASSWORD = "原密码不正确"
    TOO_SHORT = f"新密码至少 {MIN_PASSWORD} 位"
    UNCHANGED = "新密码不能与原密码相同"


class PasswordChangeRejected(ValueError):
    """业务拒绝和账户文件损坏属于不同的错误类型。"""

    def __init__(self, reason: PasswordRejection) -> None:
        """只保存预定义的业务原因。"""
        self.reason = reason
        super().__init__(reason.value)


def _path() -> Path:
    return config_dir("console") / "account.json"


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), _ITERATIONS).hex()


def _read() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("控制台账户必须为对象")
    return data


def _write(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _new_password(length: int = 14) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def ensure_account() -> dict[str, Any]:
    """账户不存在时创建 root 并把随机初始密码打到启动日志。"""
    data = _read()
    if data.get("username") and data.get("salt") and data.get("password_hash"):
        return data
    if _path().exists():
        raise ValueError("已有控制台账户不完整，拒绝覆盖")
    password = _new_password()
    salt = secrets.token_hex(16)
    data = {
        "username": USERNAME,
        "salt": salt,
        "password_hash": _hash(password, salt),
        "must_change_password": True,
    }
    _write(data)
    logger.warning(f"[Console] 已生成控制台初始账户 {USERNAME}，初始密码：{password}（首次登录后请立即修改）")
    return data


def verify(username: str, password: str) -> bool:
    """校验用户名密码。"""
    data = ensure_account()
    expected_name = str(data.get("username") or "")
    if not hmac.compare_digest(str(username).strip(), expected_name):
        return False
    expected = str(data.get("password_hash") or "")
    return hmac.compare_digest(_hash(str(password), str(data.get("salt") or "")), expected)


def profile() -> dict[str, Any]:
    """登录态需要的账户信息。"""
    data = ensure_account()
    return {
        "username": str(data.get("username") or USERNAME),
        "must_change_password": bool(data.get("must_change_password")),
    }


def change_password(old_password: str, new_password: str) -> None:
    """校验旧密码后写入新密码；首次改密同时清掉提醒标记。"""
    data = ensure_account()
    if not verify(str(data.get("username") or ""), old_password):
        raise PasswordChangeRejected(PasswordRejection.WRONG_PASSWORD)
    if len(new_password) < MIN_PASSWORD:
        raise PasswordChangeRejected(PasswordRejection.TOO_SHORT)
    if new_password == old_password:
        raise PasswordChangeRejected(PasswordRejection.UNCHANGED)
    salt = secrets.token_hex(16)
    data.update(
        salt=salt,
        password_hash=_hash(new_password, salt),
        must_change_password=False,
    )
    _write(data)
