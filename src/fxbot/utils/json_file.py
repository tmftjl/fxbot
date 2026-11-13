"""运行数据 JSON 的明确读取与原子写入。"""

from __future__ import annotations

import os
import json
from typing import Any
from pathlib import Path
from tempfile import NamedTemporaryFile
from collections.abc import Callable


def read_json(path: Path, default: Callable[[], Any]) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default()
    return json.loads(text)


def write_json(path: Path, value: Any) -> None:
    """校验并写入同目录临时文件，失败不能覆盖原始状态。"""
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix="state-", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
