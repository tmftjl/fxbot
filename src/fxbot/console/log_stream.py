"""应用显式拥有的日志环形缓冲与订阅分发。"""

from __future__ import annotations

import sys
import logging
import secrets
import threading
import traceback
from typing import TYPE_CHECKING, Any
from asyncio import Queue, get_running_loop
from datetime import datetime, timezone
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
        _append(record["time"], record["level"].name, record["name"] or "", str(message))
    except Exception:
        traceback.print_exc(file=sys.stderr)


class _StandardSink(logging.Handler):
    """采集核心和 ASGI 服务的标准日志，不影响其他处理器。"""

    def emit(self, record: logging.LogRecord) -> None:
        """日志入口必须独立报告自身的处理异常。"""
        try:
            _append(
                datetime.fromtimestamp(record.created, timezone.utc), record.levelname, record.name, self.format(record)
            )
        except Exception:
            self.handleError(record)


_standard_sink = _StandardSink(level=logging.INFO)


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
    _sink_id = logger.add(
        _sink,
        level="INFO",
        format="{message}",
        colorize=False,
        backtrace=False,
        diagnose=False,
    )
    logging.getLogger().addHandler(_standard_sink)


def stop() -> None:
    """卸载本应用的处理器并释放订阅，不修改其他日志所有者。"""
    global _sink_id, _loop
    if _sink_id is not None:
        logger.remove(_sink_id)
        _sink_id = None
    logging.getLogger().removeHandler(_standard_sink)
    with _lock:
        _subscribers.clear()
        _loop = None
