"""视频解析的完整群会话开关。"""

from __future__ import annotations

from ...core import SessionTarget
from ...utils.paths import data_dir
from ...utils.json_file import read_json, write_json

STATE_PATH = data_dir("video_parser") / "groups.json"


def load_state() -> dict:
    data = read_json(STATE_PATH, lambda: {"disabled_groups": []})
    if not isinstance(data, dict) or set(data) != {"disabled_groups"} or not isinstance(data["disabled_groups"], list):
        raise ValueError("视频解析群开关格式无效")
    for key in data["disabled_groups"]:
        if not isinstance(key, str) or SessionTarget.from_key(key).kind != "group":
            raise ValueError("视频解析必须使用完整群会话键；旧裸群号不能猜测归属")
    return data


def is_group_enabled(target: SessionTarget) -> bool:
    return target.kind != "group" or target.key() not in load_state()["disabled_groups"]


def set_group_enabled(target: SessionTarget, enabled: bool) -> None:
    if target.kind != "group" or type(enabled) is not bool:
        raise ValueError("群解析开关目标无效")
    groups = set(load_state()["disabled_groups"])
    if enabled:
        groups.discard(target.key())
    else:
        groups.add(target.key())
    write_json(STATE_PATH, {"disabled_groups": sorted(groups)})
