"""gsuid_core WebSocket 连接器：连接、收发、退避重连与关闭清理。"""

from __future__ import annotations

import asyncio
from typing import Any, Callable
from dataclasses import dataclass
from urllib.parse import quote

from loguru import logger
from websockets.exceptions import WebSocketException
from websockets.asyncio.client import connect

from ...core import ApplicationServices
from .models import MessageSend, MessageReceive, decode_send, encode_receive, recall_receipt
from .dispatch import dispatch_outgoing
from ...core.plugin import Service

INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0
CONNECT_TIMEOUT = 60.0
MAX_FRAME_SIZE = 2**26
QUEUE_SIZE = 1024


def _host_without_scheme(host: str) -> str:
    value = host.strip()
    for prefix in ("ws://", "wss://", "http://", "https://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    value = value.rstrip("/")
    if not value:
        raise ValueError("gscore host 不能为空")
    return value


def build_ws_url(host: str, port: int, bot_id: str, token: str = "") -> str:
    """构造 ws://{host}:{port}/ws/{bot_id}[?token=...]。"""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("gscore port 必须位于 1 到 65535")
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise ValueError("gscore bot_id 不能为空")
    url = f"ws://{_host_without_scheme(host)}:{port}/ws/{quote(bot_id, safe='')}"
    if token:
        url += "?token=" + quote(str(token), safe="")
    return url


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    """连接相关配置；reply_image 变化不需要重建连接。"""

    enabled: bool
    host: str
    port: int
    token: str
    bot_id: str

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ConnectionSettings":
        port = config.get("port", 8765)
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("gscore port 必须为整数")
        return cls(
            enabled=bool(config.get("enabled", True)),
            host=str(config.get("host", "localhost")),
            port=port,
            token=str(config.get("token", "")),
            bot_id=str(config.get("bot_id", "FxBot")),
        )

    @property
    def url(self) -> str:
        return build_ws_url(self.host, self.port, self.bot_id, self.token)

    @property
    def display_url(self) -> str:
        """日志中不输出 token。"""
        return build_ws_url(self.host, self.port, self.bot_id)


class GscoreService(Service):
    """应用级业务对象：唯一 WS 连接、上行队列和受本对象管理的重连任务。"""

    def __init__(
        self,
        runtime: ApplicationServices,
        *,
        connector: Callable[..., Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
        initial_backoff: float = INITIAL_BACKOFF,
        max_backoff: float = MAX_BACKOFF,
        connect_timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self.runtime = runtime
        self._connector = connector if connector is not None else connect
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._connect_timeout = connect_timeout
        self._queue: asyncio.Queue[MessageReceive] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._settings: ConnectionSettings | None = None
        self._closed = False
        self.attempts = 0

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._task is not None and not self._task.done()

    @property
    def settings(self) -> ConnectionSettings | None:
        return self._settings

    async def maintain(self, config: dict[str, Any]) -> None:
        """定时任务调用：保持唯一连接；配置变化时先停旧任务再重连。"""
        if self._closed:
            return
        settings = ConnectionSettings.from_config(config)
        if not settings.enabled:
            self._settings = settings
            await self.stop_connection()
            return
        if self._task is not None and not self._task.done() and self._settings == settings:
            return
        await self.stop_connection()
        self._settings = settings
        self._task = self.runtime.application.spawn(self._run(settings), name="gscore.connection")

    async def _run(self, settings: ConnectionSettings) -> None:
        delay = self._initial_backoff
        while True:
            self.attempts += 1
            try:
                async with self._connector(
                    settings.url,
                    max_size=MAX_FRAME_SIZE,
                    open_timeout=self._connect_timeout,
                    ping_interval=20,
                    ping_timeout=60,
                ) as ws:
                    self._ws = ws
                    delay = self._initial_backoff
                    logger.info("[gscore] 已连接 gsuid_core: {}", settings.display_url)
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, WebSocketException) as exc:
                logger.warning("[gscore] 连接失败或断开({}): {}", settings.display_url, exc)
            finally:
                self._ws = None
            await self._sleep(delay)
            delay = min(delay * 2, self._max_backoff)

    async def _pump(self, ws: Any) -> None:
        receive = asyncio.create_task(self._receive_loop(ws), name="gscore.receive")
        send = asyncio.create_task(self._send_loop(ws), name="gscore.send")
        try:
            await asyncio.wait({receive, send}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (receive, send):
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(receive, send, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = decode_send(raw)
            except (ValueError, TypeError) as exc:
                logger.warning("[gscore] 忽略无法解析的下行帧: {}", exc)
                continue
            await self._process(msg)

    async def _process(self, msg: MessageSend) -> None:
        recall_id: str | list[str] | None = None
        try:
            result = await dispatch_outgoing(self.runtime, msg)
            if result is not None:
                recall_id = result.recall_id
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).error("[gscore] 处理下行帧失败 bot_id={} target={}", msg.bot_id, msg.target_id)
        finally:
            if msg.echo:
                if not self.submit(recall_receipt(msg, recall_id)):
                    logger.warning("[gscore] 回执未能入队: echo={}", msg.echo)

    async def _send_loop(self, ws: Any) -> None:
        while True:
            message = await self._queue.get()
            try:
                await ws.send(encode_receive(message))
            finally:
                self._queue.task_done()

    def submit(self, message: MessageReceive) -> bool:
        """把上行事件放入发送队列；未连接时丢弃而不是无限堆积。"""
        if not self.connected:
            logger.debug("[gscore] 当前未连接 gsuid_core，上行事件已丢弃: {}", message.bot_id)
            return False
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning("[gscore] 上行队列已满，事件已丢弃: {}", message.bot_id)
            return False
        return True

    async def stop_connection(self) -> None:
        """取消并等待连接任务，确保关闭时没有遗留后台任务。"""
        task, self._task = self._task, None
        self._ws = None
        if task is None:
            return
        if not task.done():
            self.runtime.application.cancel(task)
        results = await asyncio.gather(task, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.error("[gscore] 连接任务异常退出: {}", result)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.stop_connection()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
