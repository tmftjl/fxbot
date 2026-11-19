"""QQ 官方 WebSocket 网关的连接、鉴权、心跳与重连。"""

from __future__ import annotations

import sys
import json
import asyncio
from collections import OrderedDict
from dataclasses import replace

import httpx
from loguru import logger
from pydantic import TypeAdapter
from websockets.asyncio.client import connect

from .api import QQHTTP
from .client import QQClient
from .events import QQEventStream
from .models import Hello, Dispatch, Reconnect, PayloadType, HeartbeatAck, InvalidSession, ShardUrlGetReturn
from .signing import QQSigner
from .event_models import ReadyEvent
from ...core.application import ApplicationState
from ...core.concurrency import run_concurrently

RECONNECT_INTERVAL = 3.0
_PAYLOAD = TypeAdapter(PayloadType)


class GatewayProtocolError(ValueError):
    """网关帧结构或状态无效；由连接循环记录并重连。"""


class GatewayReconnect(Exception):
    """平台要求重新建立连接。"""


def decode_frame(raw):
    try:
        return _PAYLOAD.validate_json(raw)
    except ValueError as error:
        raise GatewayProtocolError("QQ 网关帧无效") from error


def sequence(frame):
    return frame.get("s")


class QQGateway:
    def __init__(self, application, app_id, config, settings, *, connector=connect, owner=None):
        self.application, self.app_id, self.config, self.settings = application, app_id, config, settings
        self.signer = QQSigner(config.secret)
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
        self._session_id = None
        self._received = None
        self._reply_sequences = OrderedDict()
        self._bot_user_id = config.bot_user_id
        self._bot_username = None
        self._owner = owner if owner is not None else self
        self._children = ()
        self._online_shards = set()
        self._shard = config.shard
        self._shard_count = 1
        self._concurrency = 1
        self._gateway_url = None

    def start(self):
        if self._runner is not None or self._stopped:
            raise RuntimeError("QQ 网关不能重复启动")
        self._runner = self.application.spawn(self.run(), name=f"qq:{self.app_id}:gateway")

    def _reset_session(self):
        self._session_id = None
        self._received = None

    async def _pause(self, seconds):
        await asyncio.sleep(seconds)

    async def _gateway(self):
        data = ShardUrlGetReturn.model_validate(await self.api.request("GET", "/gateway/bot"))
        self._shard_count = data.shards or 1
        self._shard = self.config.shard if self.config.shard is not None else (0, self._shard_count)
        limit = data.session_start_limit
        remaining = limit.remaining
        if remaining and remaining <= 0:
            raise GatewayProtocolError("QQ 会话启动额度不足")
        self._concurrency = limit.max_concurrency or 1
        return self.config.gateway_url or data.url

    async def _send(self, socket, frame):
        await socket.send(json.dumps(frame, separators=(",", ":")))

    def _activate(self):
        if self._stream is not None:
            return
        if self._owner is not self:
            self._owner._bot_user_id = self._bot_user_id
            self._owner._bot_username = self._bot_username
            self._owner._activate()
            self._owner._online_shards.add(self)
            self._stream = self._owner._stream
            return
        if self._event_stream is None:
            client = QQClient(
                self.app_id, self.api, sequences=self._reply_sequences, available=lambda: not self._stopped
            )
            self._event_stream = QQEventStream(self.application, client, bot_user_id=self._bot_user_id)
        self._event_stream.bot_user_id = self._bot_user_id
        self._event_stream.bot_username = self._bot_username
        self._event_stream.start(self.close)
        self._stream = self._event_stream

    def _dispatch(self, frame):
        self._received = sequence(frame)
        self._stream.receive(frame)

    def receive(self, payload):
        if self._stream is None:
            self._activate()
        self._dispatch(payload)

    async def _heartbeat(self, socket, interval):
        while True:
            if self._session_id is not None:
                try:
                    await self._send(socket, {"op": 1, "d": self._received})
                except Exception:
                    logger.opt(exception=True).warning("QQ 心跳发送失败，忽略本次心跳")
            await asyncio.sleep(interval / 1000)

    async def session(self, socket):
        hello = decode_frame(await socket.recv())
        if not isinstance(hello, Hello):
            raise GatewayProtocolError("QQ 未先发送 Hello")
        interval = hello.data.heartbeat_interval
        token = await self.api.token()
        ready = None
        if self._session_id is not None:
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
            await self._send(
                socket,
                {
                    "op": 2,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": self.config.intents,
                        "shard": list(self._shard),
                        "properties": {"$os": sys.platform, "$language": f"python {sys.version}", "$sdk": "FxBot"},
                    },
                },
            )
            ready = decode_frame(await socket.recv())
            if isinstance(ready, InvalidSession):
                raise GatewayReconnect("QQ 会话无效")
            if not isinstance(ready, Dispatch) or ready.type != "READY":
                raise GatewayProtocolError("QQ Identify 后未先收到 READY")
            ready = ready.model_dump(by_alias=True, mode="json")
            self._received = sequence(ready)
            data = ReadyEvent.model_validate({"event_id": ready.get("id"), **ready["d"]})
            self._session_id = data.session_id
            self._bot_user_id = data.user.id
            self._bot_username = data.user.username
        self._activate()
        heartbeat = asyncio.create_task(self._heartbeat(socket, interval), name=f"qq:{self.app_id}:heartbeat")
        try:
            if ready is not None:
                self._stream.receive(ready)
            while True:
                frame = decode_frame(await socket.recv())
                if isinstance(frame, Dispatch):
                    self._dispatch(frame.model_dump(by_alias=True, mode="json"))
                elif isinstance(frame, Reconnect):
                    raise GatewayReconnect("QQ 平台要求重连")
                elif isinstance(frame, InvalidSession):
                    self._reset_session()
                    raise GatewayReconnect("QQ 会话无效，重新 Identify")
                elif isinstance(frame, HeartbeatAck):
                    logger.trace("QQ Heartbeat ACK")
                else:
                    logger.warning("QQ 未知网关帧: {}", frame)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

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
        try:
            try:
                url = self._gateway_url or await self._gateway()
            except Exception:
                logger.exception("QQ 获取网关信息失败")
                return
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
                for child in self._children:
                    child._gateway_url = url

                async def start_child(index, child):
                    await self._pause(index * self._concurrency)
                    await child.run()

                await run_concurrently(start_child(i, child) for i, child in enumerate(self._children))
                return
            while not self._stopped and self.application.state in {ApplicationState.STARTING, ApplicationState.RUNNING}:
                try:
                    async with self._connector(
                        url,
                        proxy=None,
                        ping_interval=None,
                        open_timeout=30,
                        close_timeout=30,
                        max_size=None,
                        max_queue=128,
                    ) as socket:
                        self._socket = socket
                        try:
                            await self.session(socket)
                        finally:
                            await socket.close(code=1000 if self._stopped else 1001)
                except GatewayReconnect as error:
                    logger.info("QQ 网关重新连接: {}", error)
                except Exception:
                    logger.exception("QQ 网关处理失败，尝试重新连接")
                finally:
                    self._socket = None
                    await self._disconnect()
                await self._pause(RECONNECT_INTERVAL)
        finally:
            await self._disconnect()

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
            self._reply_sequences.clear()
            await self.api.close()
