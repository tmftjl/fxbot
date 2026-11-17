"""远行商人订阅状态存储。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from pathlib import Path

from loguru import logger

from ...utils.paths import data_dir


class RocomStore:
    """保持既有 state.json 字段，订阅目标额外带上精确连接身份。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        return data_dir("rocom") / "state.json"

    @staticmethod
    def _default() -> dict[str, Any]:
        return {"last_signature": "", "subscriptions": []}

    def _read(self) -> dict[str, Any]:
        """从磁盘读取订阅状态。"""
        path = self.path
        if not path.exists():
            return self._default()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("last_signature", "")
                data.setdefault("subscriptions", [])
                return data
        except Exception:
            logger.opt(exception=True).warning("[rocom] 订阅状态加载失败")
        return self._default()

    def load_state(self) -> dict[str, Any]:
        """加载订阅状态，返回可安全修改的副本。"""
        return deepcopy(self._read())

    def save_state(self, data: dict[str, Any]) -> None:
        """保存订阅状态。"""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_subscriptions(self) -> list[dict[str, Any]]:
        """获取所有订阅。"""
        subs = self.load_state().get("subscriptions", [])
        return subs if isinstance(subs, list) else []

    def upsert_subscription(self, sub_type: str, sub_key: str, target: dict[str, Any], operator_id: str) -> None:
        """新增或更新订阅（群或私聊）。"""
        state = self.load_state()
        key_field = "group_key" if sub_type == "group" else "user_key"
        subs = [
            item
            for item in self.get_subscriptions()
            if not (item.get("type") == sub_type and item.get(key_field) == sub_key)
        ]
        entry: dict[str, Any] = {
            "type": sub_type,
            key_field: sub_key,
            "target": target,
            "operator_id": operator_id,
        }
        subs.append(entry)
        state["subscriptions"] = subs
        self.save_state(state)

    def remove_subscription(self, sub_type: str, sub_key: str) -> bool:
        """删除订阅（群或私聊）。"""
        state = self.load_state()
        old = self.get_subscriptions()
        key_field = "group_key" if sub_type == "group" else "user_key"
        new = [item for item in old if not (item.get("type") == sub_type and item.get(key_field) == sub_key)]
        state["subscriptions"] = new
        self.save_state(state)
        return len(new) != len(old)

    def get_subscription(self, sub_type: str, sub_key: str) -> dict[str, Any] | None:
        """获取单个订阅（群或私聊）。"""
        key_field = "group_key" if sub_type == "group" else "user_key"
        for item in self.get_subscriptions():
            if item.get("type") == sub_type and item.get(key_field) == sub_key:
                return item
        return None

    def get_last_signature(self) -> str:
        """获取最后推送签名。"""
        return str(self.load_state().get("last_signature") or "")

    def set_last_signature(self, signature: str) -> None:
        """保存最后推送签名。"""
        state = self.load_state()
        state["last_signature"] = signature
        self.save_state(state)


store = RocomStore()
