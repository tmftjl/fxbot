"""Claude Code 的 native session id 持久化，供空闲回收后 resume。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger

from ...utils.paths import data_dir

MAX_ENTRIES = 200
FILE_NAME = "sessions.json"


def _path() -> Path:
    return data_dir("claude_code") / FILE_NAME


def _load() -> dict[str, dict]:
    path = _path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _save(data: dict[str, dict]) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.opt(exception=True).debug("[ServerAdmin] 保存 Claude Code 会话索引失败")


def get(key: str) -> str:
    """读取该用户保留的 ACP session id。"""
    row = _load().get(key) or {}
    return str(row.get("session_id") or "")


def put(key: str, session_id: str) -> None:
    """记录 ACP session id，按时间保留最近若干条。"""
    if not key or not session_id:
        return
    data = _load()
    data[key] = {"session_id": session_id, "updated_at": int(time.time())}
    if len(data) > MAX_ENTRIES:
        ordered = sorted(data.items(), key=lambda item: item[1].get("updated_at", 0))
        data = dict(ordered[-MAX_ENTRIES:])
    _save(data)


def drop(key: str) -> None:
    """删除该用户保留的 ACP session id。"""
    data = _load()
    if data.pop(key, None) is not None:
        _save(data)


ENGINE_FILE = "engines.json"


def _engine_path() -> Path:
    return data_dir("claude_code") / ENGINE_FILE


def _load_engines() -> dict[str, str]:
    try:
        raw = json.loads(_engine_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}


def get_engine(key: str) -> str:
    """读取用户当前引擎，未设置返回空串。"""
    return _load_engines().get(key, "")


def put_engine(key: str, engine: str) -> None:
    """保存用户当前引擎。"""
    if not key or not engine:
        return
    data = _load_engines()
    data[key] = engine
    try:
        _engine_path().write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.opt(exception=True).debug("[ServerAdmin] 保存 Claude Code 引擎选择失败")
