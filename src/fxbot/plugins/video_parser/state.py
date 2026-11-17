"""视频解析的群号开关。"""

from __future__ import annotations

import json

from ...core import SessionTarget
from ...utils.paths import data_dir
from ...utils.json_file import read_json, write_json

STATE_PATH = data_dir("video_parser") / "groups.json"


def load_state() -> dict[str, list[str]]:
    """读取群号开关；文件只读，不在加载时改写。"""
    try:
        data = read_json(STATE_PATH, lambda: {"disabled_groups": []})
    except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
        return {"disabled_groups": []}
    if not isinstance(data, dict):
        return {"disabled_groups": []}
    groups = data.get("disabled_groups")
    return {"disabled_groups": [str(item) for item in groups]} if isinstance(groups, list) else {"disabled_groups": []}


def is_group_enabled(target: SessionTarget) -> bool:
    return target.kind != "group" or target.target_id not in set(load_state()["disabled_groups"])


def set_group_enabled(target: SessionTarget, enabled: bool) -> None:
    if target.kind != "group" or type(enabled) is not bool:
        raise ValueError("群解析开关目标无效")
    groups = set(load_state()["disabled_groups"])
    if enabled:
        groups.discard(target.target_id)
    else:
        groups.add(target.target_id)
    write_json(STATE_PATH, {"disabled_groups": sorted(groups)})
