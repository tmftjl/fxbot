"""应用显式拥有的日志环形缓冲与订阅分发。"""

from __future__ import annotations

import sys
import logging
import secrets
import threading
import traceback
from typing import TYPE_CHECKING, Any
from asyncio import Queue, get_running_loop
from datetime import datetime
from collections import deque

from loguru import logger

from ..utils.tz import as_shanghai
from .log_store import enqueue as enqueue_log

if TYPE_CHECKING:
    # loguru 只在 __init__.pyi 里声明 Message，运行时顶层仅导出 logger，只能作类型注解用
    from asyncio import AbstractEventLoop

    from loguru import Message

_BUFFER_SIZE = 2000
_QUEUE_SIZE = 1000
_TIME_FORMAT = "%m-%d %H:%M:%S"
# 进程标识：seq 每次重启都从 1 重新计数，前端靠它区分「重连补发」和「服务端已重启」
_BOOT_ID = secrets.token_hex(4)
# 实时日志采集级别：控制台缓冲区按此过滤，历史落库仍固定 INFO 起
_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")
_STDLIB_LEVELS = {
    "TRACE": 5,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "SUCCESS": 25,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_level = "INFO"
_level_no = logger.level("INFO").no

_lock = threading.Lock()
_records: deque[dict[str, Any]] = deque(maxlen=_BUFFER_SIZE)
_subscribers: set[Queue] = set()
_seq = 0
_loop: AbstractEventLoop | None = None
_sink_id: int | None = None


def _dispatch(item: dict[str, Any]) -> None:
    """把一条记录投递给所有订阅队列，必须在事件循环线程执行。"""
    for queue in _subscribers:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(item)


def _append(moment: datetime, level: str, name: str, text: str) -> None:
    """标准日志和 Loguru 使用同一条记录存储边界。"""
    global _seq
    moment = as_shanghai(moment)
    with _lock:
        _seq += 1
        item = {
            "boot": _BOOT_ID,
            "seq": _seq,
            "time": moment.strftime(_TIME_FORMAT),
            "ts": moment.isoformat(timespec="milliseconds"),
            "ts_ms": int(moment.timestamp() * 1000),
            "level": level,
            "name": name,
            "text": text.rstrip("\n"),
        }
        _records.append(item)
        loop = _loop
        has_subscriber = bool(_subscribers)
    enqueue_log(item)
    if loop is not None and has_subscriber:
        loop.call_soon_threadsafe(_dispatch, item)


def _sink(message: Message) -> None:
    """日志处理器的故障只写 stderr，不能递归写回日志本身。"""
    try:
        record = message.record
        if record["level"].no < _level_no:
            return
        _append(record["time"], record["level"].name, record["name"] or "", str(message))
    except Exception:
        traceback.print_exc(file=sys.stderr)


class _LoguruForwarder(logging.Handler):
    """把标准库日志转交给 loguru，core 保持零第三方依赖。"""

    def emit(self, record: logging.LogRecord) -> None:
        """保留原始模块与异常，转发失败交给 logging 自身的错误处理。"""
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 0
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if module != __name__ and frame.f_code.co_filename != logging.__file__:
                break
            frame = frame.f_back
            depth += 1
        try:
            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
        except Exception:
            self.handleError(record)


_loguru_forwarder = _LoguruForwarder(level=logging.INFO)


def current_level() -> str:
    """当前实时日志采集级别。"""
    return _level


def set_level(name: str) -> str:
    """调整实时日志采集级别；只影响控制台缓冲区，落库仍从 INFO 起。"""
    global _level, _level_no
    normalized = str(name or "").strip().upper()
    if normalized not in _LEVELS:
        raise ValueError("未知日志级别")
    _level = normalized
    _level_no = logger.level(normalized).no
    _loguru_forwarder.setLevel(_STDLIB_LEVELS[normalized])
    return _level


def boot_id() -> str:
    """返回本进程的日志缓冲标识。"""
    return _BOOT_ID


def snapshot(after_seq: int | None = None) -> list[dict[str, Any]]:
    """取出缓冲记录，给定 seq 时只返回其后的部分。"""
    with _lock:
        if after_seq is None:
            return list(_records)
        return [item for item in _records if item["seq"] > after_seq]


def subscribe() -> Queue:
    """注册一个订阅队列，并记下事件循环供跨线程投递使用。"""
    global _loop
    queue: Queue = Queue(maxsize=_QUEUE_SIZE)
    _loop = get_running_loop()
    with _lock:
        _subscribers.add(queue)
    return queue


def unsubscribe(queue: Queue) -> None:
    """注销订阅队列。"""
    with _lock:
        _subscribers.discard(queue)


def start() -> None:
    """显式安装一次处理器，禁止在模块导入时安装。"""
    global _sink_id
    if _sink_id is not None:
        raise RuntimeError("日志缓冲已启动")
    # 处理器挂到 TRACE，实际门槛由 _sink 按当前采集级别过滤，切换级别不用重挂
    _sink_id = logger.add(
        _sink,
        level="TRACE",
        format="{message}",
        colorize=False,
        backtrace=False,
        diagnose=False,
    )
    _loguru_forwarder.setLevel(_STDLIB_LEVELS[_level])
    logging.getLogger().addHandler(_loguru_forwarder)


def stop() -> None:
    """卸载本应用的处理器并释放订阅，不修改其他日志所有者。"""
    global _sink_id, _loop, _level, _level_no
    if _sink_id is not None:
        logger.remove(_sink_id)
        _sink_id = None
    logging.getLogger().removeHandler(_loguru_forwarder)
    _level = "INFO"
    _level_no = logger.level("INFO").no
    _loguru_forwarder.setLevel(_STDLIB_LEVELS[_level])
    with _lock:
        _subscribers.clear()
        _loop = None
