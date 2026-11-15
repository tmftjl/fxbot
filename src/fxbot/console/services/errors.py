"""管理用例的错误与下载结果，不依赖 Web 框架。"""

from enum import Enum
from pathlib import Path
from dataclasses import dataclass


class AdminFailure(Enum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class MemoryAdminError(Exception):
    def __init__(self, *, kind: AdminFailure, detail: str):
        super().__init__(detail)
        self.kind, self.detail = kind, detail


@dataclass(frozen=True, slots=True)
class Download:
    path: Path
    media_type: str = "application/octet-stream"
    filename: str | None = None
