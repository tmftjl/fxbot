"""QQ 官方 WebSocket 网关状态机；HTTP 消息写入不随重连自动重试。"""

from __future__ import annotations

import sys
import json
import asyncio
import logging
from contextlib import AsyncExitStack
from collections import OrderedDict
from urllib.parse import urlsplit

import httpx
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.asyncio.client import connect

from .api import QQHTTP, QQAPIError
from .codec import opaque_id, identifier
from .client import QQClient
from .events import QQEventStream, QQEventOverflow
from ...core.application import ApplicationState

logger = logging.getLogger(__name__)


class GatewayProtocolError(ValueError):
    """结构或状态与网关协议不符，不通过猜测字段重试。"""


class GatewayReconnect(Exception):
    """官方要求重连或心跳失效，与业务调用重试无关。"""


def decode_frame(raw):
    if not isinstance(raw, str):
        raise GatewayProtocolError("QQ 网关必须发送 JSON 文本帧")
    try:
        frame = json.loads(raw)
    except ValueError:
        raise GatewayProtocolError("QQ 网关帧不是有效 JSON") from None
    if not isinstance(frame, dict) or type(frame.get("op")) is not int:
        raise GatewayProtocolError("QQ 网关帧缺少有效 op")
    return frame


def sequence(frame):
    value = frame.get("s")
    if type(value) is not int or value < 0:
        raise GatewayProtocolError("QQ Dispatch 缺少有效序列号")
    return value


class QQGateway:
    def __init__(self, application, app_id, config, settings, *, connector=connect):
        self.application, self.app_id, self.config, self.settings = application, app_id, config, settings
        self.api = QQHTTP(
            app_id,
            config.secret,
            httpx.AsyncClient(timeout=settings.action_timeout, follow_redirects=False, trust_env=False),
        )
        self._connector = connector
        self._runner = None
        self._close_task = None
        self._stopped = False
        self._stream = None
        self._socket = None
        self._session_id = ""
        self._received = None
        self._committed = None
        self._pending = OrderedDict()
        self._seen = OrderedDict()
        self._reply_sequences = OrderedDict()
        self._bot_user_id = config.bot_user_id
        self._ready_this_attempt = False
        self._budget_remaining = None
        self._budget_reset_at = 0.0
        self._last_identify = 0.0
        self._shard = None

    def start(self):
        if self._runner is not None or self._stopped:
            raise RuntimeError("QQ 网关不能重复启动")
        self._runner = self.application.spawn(self.run(), name=f"qq:{self.app_id}:gateway")

    def _reset_session(self):
        self._session_id = ""
        self._received = None
        self._committed = None
        self._pending.clear()

    async def _pause(self, seconds):
        await asyncio.sleep(seconds)

    async def _gateway(self):
        data = await self.api.request("GET", "/gateway/bot")
        url = data.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password:
            raise GatewayProtocolError("QQ 网关地址必须是官方返回的 WSS URL")
        shards = data.get("shards")
        if type(shards) is not int or shards < 1:
            raise GatewayProtocolError("QQ 网关缺少分片总数")
        if self.config.shard is None and shards != 1:
            raise GatewayProtocolError("QQ 网关要求分片，请明确配置本进程负责的 shard")
        self._shard = self.config.shard if self.config.shard is not None else (0, 1)
        budget = data.get("session_start_limit")
        if not isinstance(budget, dict):
            raise GatewayProtocolError("QQ 网关缺少 session_start_limit")
        for field in ("remaining", "reset_after", "max_concurrency"):
            if type(budget.get(field)) is not int or budget[field] < 0:
                raise GatewayProtocolError("QQ 会话启动限额无效")
        if budget["reset_after"] <= 0 or budget["max_concurrency"] <= 0:
            raise GatewayProtocolError("QQ 会话额度缺少有效的重置时间或并发额度")
        now = asyncio.get_running_loop().time()
        # 官方额度可能短暂滞后；本地已经用掉的 Identify 不能因重新 GET 而补回来。
        if self._budget_remaining is None or now >= self._budget_reset_at:
            self._budget_remaining = budget["remaining"]
            self._budget_reset_at = now + budget["reset_after"] / 1000
        else:
            self._budget_remaining = min(self._budget_remaining, budget["remaining"])
        return url

    async def _identify_limit(self):
        now = asyncio.get_running_loop().time()
        if self._budget_remaining == 0:
            delay = self._budget_reset_at - now
            if delay <= 0:
                raise GatewayProtocolError("QQ 会话启动额度为零且未提供有效重置时间")
            logger.warning("QQ Identify 额度已用尽，等待 %.1f 秒后重新获取", delay)
            await self._pause(delay)
            raise GatewayReconnect("QQ 会话额度重置后重新获取网关")
        delay = 5 - (now - self._last_identify) if self._last_identify else 0
        if delay > 0:
            await self._pause(delay)

    async def _send(self, socket, frame):
        await asyncio.wait_for(
            socket.send(json.dumps(frame, separators=(",", ":"))), timeout=self.settings.action_timeout
        )

    def _activate(self):
        if self._stream is not None:
            return
        stream = None
        client = QQClient(
            self.app_id,
            self.api,
            sequences=self._reply_sequences,
            available=lambda: (
                not self._stopped
                and stream is not None
                and not stream.closed
                and self.application.is_registered(stream.client)
            ),
        )
        stream = QQEventStream(
            self.application, client, bot_user_id=self._bot_user_id, capacity=self.settings.queue_size, seen=self._seen
        )
        stream.start(self.close)
        self._stream = stream
        self._ready_this_attempt = True

    def _processed(self, seq):
        if seq in self._pending:
            self._pending[seq] = True
        while self._pending and next(iter(self._pending.values())):
            value, _ = self._pending.popitem(last=False)
            self._committed = value

    def _dispatch(self, frame):
        seq = sequence(frame)
        if self._committed is not None and seq <= self._committed:
            return
        if self._received is not None and seq < self._received:
            raise GatewayProtocolError("QQ Dispatch 序列号倒退")
        if seq in self._pending:
            return
        if len(self._pending) >= self.settings.queue_size + 1:
            raise GatewayReconnect("QQ 待处理事件达到容量")
        self._pending[seq] = False
        try:
            self._stream.receive(frame, processed=lambda: self._processed(seq))
        except QQEventOverflow:
            self._pending.pop(seq, None)
            raise GatewayReconnect("QQ 事件队列已满") from None
        self._received = seq

    async def session(self, socket):
        self._received = self._committed
        hello = decode_frame(await asyncio.wait_for(socket.recv(), self.settings.action_timeout))
        data = hello.get("d")
        interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
        if hello["op"] != 10 or type(interval) is not int or not 10 <= interval <= 300000:
            raise GatewayProtocolError("QQ Hello 缺少有效心跳间隔")
        token = await self.api.token()
        resuming = bool(self._session_id)
        if resuming:
            await self._send(
                socket,
                {
                    "op": 6,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": self._session_id,
                        "seq": self._committed,
                    },
                },
            )
        else:
            await self._identify_limit()
            self._budget_remaining -= 1
            self._last_identify = asyncio.get_running_loop().time()
            await self._send(
                socket,
                {
                    "op": 2,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": self.config.intents,
                        "shard": list(self._shard),
                        "properties": {"$os": sys.platform, "$language": "python", "$sdk": "FxBot"},
                    },
                },
            )
        loop = asyncio.get_running_loop()
        authenticate_until = loop.time() + self.settings.action_timeout
        heartbeat_at = None
        awaiting_ack = False
        authenticated = False
        while True:
            if self._stream is not None and self._stream.worker.done():
                raise GatewayProtocolError("QQ 网关事件消费者已停止")
            deadline = heartbeat_at if authenticated else authenticate_until
            try:
                frame = decode_frame(await asyncio.wait_for(socket.recv(), max(0, deadline - loop.time())))
            except asyncio.TimeoutError:
                if not authenticated:
                    raise GatewayReconnect("QQ 网关鉴权等待超时") from None
                if awaiting_ack:
                    raise GatewayReconnect("QQ 心跳 ACK 超时") from None
                await self._send(socket, {"op": 1, "d": self._received})
                awaiting_ack = True
                heartbeat_at = loop.time() + interval / 1000
                continue
            op = frame["op"]
            if op == 11:
                awaiting_ack = False
            elif op == 7:
                raise GatewayReconnect("QQ 平台要求重连")
            elif op == 9:
                self._reset_session()
                raise GatewayReconnect("QQ 会话无效，重新 Identify")
            elif op == 1:
                if self._received is None:
                    raise GatewayProtocolError("QQ 在建立会话前要求心跳")
                await self._send(socket, {"op": 1, "d": self._received})
                awaiting_ack = True
                if authenticated and heartbeat_at is None:
                    heartbeat_at = loop.time() + interval / 1000
            elif op == 0:
                kind = frame.get("t")
                if kind == "READY":
                    if resuming or authenticated:
                        raise GatewayProtocolError("QQ 在非 Identify 状态发送 READY")
                    data = frame.get("d")
                    if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
                        raise GatewayProtocolError("QQ READY 缺少 Bot 身份")
                    session_id = opaque_id(data.get("session_id"))
                    bot_user_id = identifier(data["user"].get("id"))
                    if self.config.bot_user_id and self.config.bot_user_id != bot_user_id:
                        raise GatewayProtocolError("QQ READY 用户身份与配置不一致")
                    self._session_id = session_id
                    self._bot_user_id = bot_user_id
                    self._received = self._committed = sequence(frame)
                    self._activate()
                    authenticated = True
                    heartbeat_at = loop.time() + interval / 1000
                elif resuming:
                    # 官方会先回放遗漏 Dispatch，最后才发送 RESUMED。
                    self._activate()
                    if kind == "RESUMED":
                        seq = sequence(frame)
                        if self._received is not None and seq < self._received:
                            raise GatewayProtocolError("QQ RESUMED 序列号倒退")
                        if seq not in self._pending and (self._committed is None or seq > self._committed):
                            self._pending[seq] = True
                        self._processed(None)
                        self._received = seq
                        authenticated = True
                        resuming = False
                        heartbeat_at = loop.time() + interval / 1000
                    else:
                        self._dispatch(frame)
                elif authenticated:
                    if kind == "RESUMED":
                        raise GatewayProtocolError("QQ 未请求 Resume 却收到 RESUMED")
                    self._dispatch(frame)
                else:
                    raise GatewayProtocolError("QQ Identify 后未先收到 READY")
            else:
                raise GatewayProtocolError("QQ 网关发来不支持的 opcode")

    async def _disconnect(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            await stream.close()
        self._pending.clear()
        self._received = self._committed

    async def run(self):
        failures = 0
        try:
            while not self._stopped and self.application.state in {ApplicationState.STARTING, ApplicationState.RUNNING}:
                self._ready_this_attempt = False
                try:
                    url = await self._gateway()
                    if not self._session_id:
                        await self._identify_limit()
                    async with self._connector(
                        url,
                        proxy=None,
                        ping_interval=None,
                        open_timeout=self.settings.action_timeout,
                        close_timeout=self.settings.close_timeout,
                        max_size=1024 * 1024,
                        max_queue=self.settings.queue_size,
                    ) as socket:
                        self._socket = socket
                        try:
                            await self.session(socket)
                        finally:
                            self._socket = None
                            async with AsyncExitStack() as cleanup:
                                cleanup.push_async_callback(socket.close, code=1000 if self._stopped else 1001)
                                cleanup.push_async_callback(self._disconnect)
                except GatewayReconnect as error:
                    logger.info("QQ 网关重新连接: %s", error)
                except ConnectionClosed as error:
                    code = error.rcvd.code if error.rcvd is not None else None
                    if code in {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}:
                        raise GatewayProtocolError(f"QQ 网关拒绝连接（code={code}），需要修正配置") from None
                    if code in {4006, 4007} or (code is not None and 4900 <= code <= 4913):
                        self._reset_session()
                    elif code is not None and code >= 4000 and code not in {4008, 4009}:
                        raise GatewayProtocolError(f"未识别的 QQ 网关关闭码 {code}") from None
                    logger.info("QQ 网关断开（code=%s）", code)
                except QQAPIError as error:
                    if error.status not in {429, 500, 502, 503, 504}:
                        raise
                    logger.warning("QQ 网关发现/鉴权暂不可用（HTTP %s）", error.status)
                except (OSError, asyncio.TimeoutError, httpx.TransportError, InvalidHandshake):
                    logger.warning("QQ 网关网络连接未完成")
                finally:
                    await self._disconnect()
                failures = 0 if self._ready_this_attempt else min(failures + 1, 5)
                await self._pause(min(30, 2**failures))
        finally:
            self._stopped = True
            async with AsyncExitStack() as cleanup:
                cleanup.callback(self._seen.clear)
                cleanup.callback(self._reply_sequences.clear)
                cleanup.push_async_callback(self.api.close)
                cleanup.push_async_callback(self._disconnect)

    async def close(self):
        if self._close_task is None:
            self._stopped = True
            self._close_task = asyncio.create_task(self._close(), name=f"qq:{self.app_id}:gateway-close")
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self._runner is not None:
            self.application.cancel(self._runner)
            await asyncio.gather(self._runner, return_exceptions=True)
        await self._disconnect()
        await self.api.close()
