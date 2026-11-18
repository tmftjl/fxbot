"""测试用 loguru 日志捕获，不修改被测模块的 logger。"""

from __future__ import annotations

from contextlib import contextmanager

from loguru import logger


@contextmanager
def capture_loguru(level: str = "INFO", name: str = "fxbot.core.router"):
    """捕获指定模块从给定级别起的日志文本。"""
    records: list[str] = []

    def sink(message) -> None:
        if message.record["name"] == name:
            records.append(str(message).rstrip("\n"))

    sink_id = logger.add(
        sink,
        level=level,
        format="{message}\n{exception}",
        colorize=False,
        backtrace=False,
        diagnose=False,
    )
    try:
        yield records
    finally:
        logger.remove(sink_id)
