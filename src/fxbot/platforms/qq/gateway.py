"""QQ 官方 WebSocket 网关状态机；HTTP 消息写入不随重连自动重试。"""

from __future__ import annotations

import sys
import json
import asyncio
from contextlib import AsyncExitStack
from collections import OrderedDict
from dataclasses import replace
from urllib.parse import urlsplit

import httpx
from loguru import logger
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.asyncio.client import connect

from .api import QQHTTP, QQAPIError
from .codec import opaque_id, identifier
from .client import QQClient
from .events import QQEventStream
from ...core.application import ApplicationState
from ...core.concurrency import run_concurrently


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
    def __init__(self, application, app_id, config, settings, *, connector=connect, owner=None):
        self.application, self.app_id, self.config, self.settings = application, app_id, config, settings
        self.api = (
            owner.api
            if owner is not None
            else QQHTTP(
                app_id,
                config.secret,
                httpx.AsyncClient(timeout=settings.action_timeout, follow_redirects=False, trust_env=False),
                api_base=config.api_base,
                auth_url=config.auth_url,
            )
        )
        self._connector = connector
        self._runner = None
        self._close_task = None
        self._stopped = False
        self._stream = None
        self._event_stream = None
        self._socket = None
        self._session_id = ""
        self._received = None
        self._reply_sequences = OrderedDict()
        self._bot_user_id = config.bot_user_id
        self._ready_this_attempt = False
        self._budget_remaining = None
        self._budget_reset_at = 0.0
        self._last_identify = 0.0
        self._shard = None
        self._shard_count = 1
        self._owner = owner if owner is not None else self
        self._children: tuple[QQGateway, ...] = ()
        self._online_shards = set()
        self._identify_lock = owner._identify_lock if owner is not None else asyncio.Lock()

    def start(self):
        if self._runner is not None or self._stopped:
            raise RuntimeError("QQ 网关不能重复启动")
        self._runner = self.application.spawn(self.run(), name=f"qq:{self.app_id}:gateway")

    def _reset_session(self):
        self._session_id = ""
        self._received = None

    async def _pause(self, seconds):
        await asyncio.sleep(seconds)

    async def _gateway(self):
        data = await self.api.request("GET", "/gateway/bot")
        url = self.config.gateway_url if self.config.gateway_url is not None else data.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password:
            raise GatewayProtocolError("QQ 网关地址必须是官方返回的 WSS URL")
        shards = data.get("shards")
        if type(shards) is not int or shards < 1:
            raise GatewayProtocolError("QQ 网关缺少分片总数")
        self._shard_count = shards
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
        owner = self._owner
        if owner._budget_remaining is None or now >= owner._budget_reset_at:
            owner._budget_remaining = budget["remaining"]
            owner._budget_reset_at = now + budget["reset_after"] / 1000
        else:
            owner._budget_remaining = min(owner._budget_remaining, budget["remaining"])
        return url

    async def _identify_limit(self):
        if self._owner is not self:
            return await self._owner._identify_limit()
        now = asyncio.get_running_loop().time()
        if self._budget_remaining == 0:
            delay = self._budget_reset_at - now
            if delay <= 0:
                raise GatewayProtocolError("QQ 会话启动额度为零且未提供有效重置时间")
            logger.warning("QQ Identify 额度已用尽，等待 {:.1f} 秒后重新获取", delay)
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
        if self._owner is not self:
            if self._owner._bot_user_id and self._owner._bot_user_id != self._bot_user_id:
                raise GatewayProtocolError("QQ 各分片返回的 Bot 身份不一致")
            self._owner._bot_user_id = self._bot_user_id
            self._owner._activate()
            self._owner._online_shards.add(self)
            self._stream = self._owner._stream
            self._ready_this_attempt = True
            return
        if self._event_stream is None:
            client = QQClient(
                self.app_id, self.api, sequences=self._reply_sequences, available=lambda: not self._stopped
            )
            self._event_stream = QQEventStream(self.application, client, bot_user_id=self._bot_user_id)
        stream = self._event_stream
        stream.bot_user_id = self._bot_user_id
        stream.start(self.close)
        self._stream = stream
        self._ready_this_attempt = True

    def _dispatch(self, frame):
        if self._stream is None:
            raise GatewayProtocolError("QQ 分发事件前必须完成鉴权")
        # 与原版 Bot.on_dispatch 一致：收到时更新 Resume/心跳序号，不等待业务完成。
        self._received = sequence(frame)
        self._stream.receive(frame)

    async def _receive_before(self, socket, deadline):
        """给已经缓冲的帧一次读取机会，超时或取消时回收接收任务。"""
        receiver = asyncio.create_task(socket.recv(), name=f"qq:{self.app_id}:receive")
        try:
            done, _ = await asyncio.wait((receiver,), timeout=max(0, deadline - asyncio.get_running_loop().time()))
            if receiver not in done:
                raise asyncio.TimeoutError
            return receiver.result()
        finally:
            if not receiver.done():
                receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    async def session(self, socket):
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
                        "seq": self._received,
                    },
                },
            )
        else:
            async with self._identify_lock:
                await self._identify_limit()
                if self._owner._budget_remaining is None or self._shard is None:
                    raise GatewayProtocolError("QQ Identify 前必须读取网关与启动额度")
                self._owner._budget_remaining -= 1
                self._owner._last_identify = asyncio.get_running_loop().time()
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
            if self._stream is not None and self._stream.closed:
                raise GatewayProtocolError("QQ 网关事件流已停止")
            deadline = heartbeat_at if authenticated else authenticate_until
            try:
                frame = decode_frame(await self._receive_before(socket, deadline))
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
                self._received = sequence(frame)
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
                    self._activate()
                    authenticated = True
                    heartbeat_at = loop.time() + interval / 1000
                elif resuming:
                    # 官方会先回放遗漏 Dispatch，最后才发送 RESUMED。
                    self._activate()
                    if kind == "RESUMED":
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
        if self._owner is not self:
            owner = self._owner
            owner._online_shards.discard(self)
            if not owner._online_shards and owner._stream is not None:
                shared, owner._stream = owner._stream, None
                shared.disconnect()
        elif stream is not None:
            stream.disconnect()

    async def run(self):
        failures = 0
        try:
            while not self._stopped and self.application.state in {ApplicationState.STARTING, ApplicationState.RUNNING}:
                self._ready_this_attempt = False
                try:
                    url = await self._gateway()
                    if self.config.shard is None and self._shard_count > 1:
                        self._children = tuple(
                            QQGateway(
                                self.application,
                                self.app_id,
                                replace(self.config, shard=(index, self._shard_count)),
                                self.settings,
                                connector=self._connector,
                                owner=self,
                            )
                            for index in range(self._shard_count)
                        )
                        await run_concurrently(child.run() for child in self._children)
                        return
                    if not self._session_id:
                        await self._identify_limit()
                    async with self._connector(
                        url,
                        proxy=None,
                        ping_interval=None,
                        open_timeout=self.settings.action_timeout,
                        close_timeout=self.settings.close_timeout,
                        max_size=1024 * 1024,
                        max_queue=128,
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
                    logger.info("QQ 网关重新连接: {}", error)
                except ConnectionClosed as error:
                    code = error.rcvd.code if error.rcvd is not None else None
                    if code in {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}:
                        raise GatewayProtocolError(f"QQ 网关拒绝连接（code={code}），需要修正配置") from None
                    if code in {4006, 4007} or (code is not None and 4900 <= code <= 4913):
                        self._reset_session()
                    elif code is not None and code >= 4000 and code not in {4008, 4009}:
                        raise GatewayProtocolError(f"未识别的 QQ 网关关闭码 {code}") from None
                    logger.info("QQ 网关断开（code={}）", code)
                except QQAPIError as error:
                    if error.status not in {429, 500, 502, 503, 504}:
                        raise
                    logger.warning("QQ 网关发现/鉴权暂不可用（HTTP {}）", error.status)
                except (OSError, asyncio.TimeoutError, httpx.TransportError, InvalidHandshake):
                    logger.warning("QQ 网关网络连接未完成")
                finally:
                    await self._disconnect()
                failures = 0 if self._ready_this_attempt else min(failures + 1, 5)
                await self._pause(min(30, 2**failures))
        finally:
            self._stopped = True
            async with AsyncExitStack() as cleanup:
                cleanup.callback(self._reply_sequences.clear)
                if self._owner is self:
                    cleanup.push_async_callback(self.api.close)
                    if self._event_stream is not None:
                        cleanup.push_async_callback(self._event_stream.close)
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
        if self._owner is self:
            if self._event_stream is not None:
                await self._event_stream.close()
            await self.api.close()
