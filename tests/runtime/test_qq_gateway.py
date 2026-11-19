"""QQ 网关状态机和本机 WebSocket 闭环；不访问真实 QQ。"""

from __future__ import annotations

import json
import asyncio
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import httpx
from websockets.frames import Close
from websockets.exceptions import ConnectionClosedError
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from fxbot.core.errors import BotOffline
from tests.runtime.test_qq import APP_ID, SECRET, payload
from fxbot.core.application import Application
from fxbot.platforms.qq.api import AUTH_URL
from fxbot.runtime.settings import Settings
from fxbot.platforms.qq.config import QQBotSettings
from fxbot.platforms.qq.gateway import QQGateway, GatewayReconnect, GatewayProtocolError, decode_frame
from fxbot.platforms.qq.service import QQService


def gateway_info(**changes):
    return {
        "url": "wss://gateway.example.test/ws",
        "shards": 1,
        "session_start_limit": {"total": 100, "remaining": 10, "reset_after": 60000, "max_concurrency": 1},
        **changes,
    }


def ready(seq=1):
    return {
        "op": 0,
        "t": "READY",
        "s": seq,
        "d": {"session_id": "session-id", "user": {"id": "BOT_USER"}, "version": 1, "shard": [0, 1]},
    }


async def _drain(stream):
    tasks = tuple(stream._tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    def feed(self, frame):
        self.incoming.put_nowait(frame)

    async def recv(self):
        frame = await self.incoming.get()
        if isinstance(frame, Exception):
            raise frame
        return json.dumps(frame) if isinstance(frame, dict) else frame

    async def send(self, data):
        self.outgoing.put_nowait(json.loads(data))

    async def close(self, code=1000):
        pass

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), 1)


class QQGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_group_notice_and_bad_event_do_not_stop_next_message(self):
        seen = []

        async def member_left(context):
            seen.append(("left", context.event.target.target_id))

        async def message(context):
            seen.append(("message", context.event.message_id))

        self.app.router.event("left", "member_left", member_left)
        self.app.router.message("next-message", message)
        socket, task, _ = await self.session()
        socket.feed(ready())
        socket.feed(
            {
                "op": 0,
                "s": 2,
                "t": "GROUP_MEMBER_REMOVE",
                "d": {
                    "group_openid": "",
                    "union_group_openid": "",
                    "member_openid": "MEMBER",
                    "timestamp": 1784276757,
                },
            }
        )
        socket.feed({"op": 0, "s": 3, "t": "GROUP_MEMBER_REMOVE", "d": {"group_openid": None}})
        good = payload(message_id="after-bad-event")
        good["s"] = 4
        socket.feed(good)
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await asyncio.wait_for(task, 1)
        await _drain(self.gateway._stream)
        self.assertEqual(seen, [("left", ""), ("message", "after-bad-event")])
        self.assertEqual(self.gateway._received, 4)
        self.assertFalse(self.gateway.api._http.is_closed)

    async def test_unexpected_receive_error_reconnects_without_closing_shared_resources(self):
        attempts = []

        @asynccontextmanager
        async def connector(url, **kwargs):
            attempts.append(url)
            self.assertFalse(self.gateway.api._http.is_closed)
            socket = Socket()
            socket.feed({"op": 10, "d": {"heartbeat_interval": 1000}})
            socket.feed(ready() if len(attempts) == 1 else {"op": 0, "t": "RESUMED", "s": 3, "d": ""})
            socket.feed(ValueError("unexpected receive failure"))
            if len(attempts) == 2:
                self.gateway._stopped = True
            yield socket

        self.gateway._connector = connector
        self.gateway._pause = AsyncMock()
        await self.gateway.run()
        self.assertEqual(len(attempts), 2)
        self.gateway.api.request.assert_awaited_once_with("GET", "/gateway/bot")
        self.assertEqual([call.args for call in self.gateway._pause.await_args_list], [(3.0,), (3.0,)])
        self.assertFalse(self.gateway.api._http.is_closed)
        await self.gateway.close()
        self.assertTrue(self.gateway.api._http.is_closed)

    async def asyncSetUp(self):
        self.app = Application()
        await self.app.start()
        self.addAsyncCleanup(self.app.stop)
        self.config = QQBotSettings(SECRET, mode="websocket")
        self.settings = Settings("test-only", qq_bots={APP_ID: self.config}, action_timeout=1)
        self.gateway = QQGateway(self.app, APP_ID, self.config, self.settings)
        self.addAsyncCleanup(self.gateway.close)
        self.gateway.api.token = AsyncMock(return_value="fake-token")
        self.gateway.api.request = AsyncMock(return_value=gateway_info())
        self.gateway._budget_remaining = 10
        self.gateway._budget_reset_at = asyncio.get_running_loop().time() + 60
        self.gateway._shard = (0, 1)

    async def session(self, *, resume=False, interval=10000):
        if resume:
            self.gateway._session_id = "session-id"
            self.gateway._received = 5
        socket = Socket()
        socket.feed({"op": 10, "d": {"heartbeat_interval": interval}})
        task = asyncio.create_task(self.gateway.session(socket))

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        auth = await socket.next()
        return socket, task, auth

    async def wait_online(self):
        for _ in range(50):
            if self.app.bots:
                return
            await asyncio.sleep(0)
        self.fail("网关没有登记 Bot")

    async def test_identify_payload_and_ready_identity(self):
        socket, task, auth = await self.session()
        self.assertEqual(auth["op"], 2)
        self.assertEqual(auth["d"]["token"], "QQBot fake-token")
        self.assertEqual(auth["d"]["intents"], self.config.intents)
        self.assertEqual(auth["d"]["shard"], [0, 1])
        self.assertEqual(auth["d"]["properties"]["$sdk"], "FxBot")
        self.assertFalse(self.app.bots)
        socket.feed(ready())
        await self.wait_online()
        self.assertEqual(self.gateway._stream.bot_user_id, "BOT_USER")
        self.assertEqual(self.gateway._received, 1)
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task

    async def test_dispatch_uses_shared_router_not_webhook_signature(self):
        seen = asyncio.Event()
        messages = []

        async def capture(context):
            messages.append(context.event)
            seen.set()

        self.app.router.message("capture", capture)
        socket, task, _ = await self.session()
        socket.feed(ready())
        frame = payload("AT_MESSAGE_CREATE", content="<@BOT_USER> /ping")
        frame["s"] = 2
        socket.feed(frame)
        await asyncio.wait_for(seen.wait(), 1)
        await _drain(self.gateway._stream)
        self.assertEqual(messages[0].message.plain_text(), "/ping")
        self.assertEqual(self.gateway._received, 2)
        self.gateway.api.request.assert_not_awaited()
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task

    async def test_heartbeat_ack_and_server_requested_heartbeat(self):
        socket, task, _ = await self.session(interval=30)
        socket.feed(ready())
        heartbeat = await socket.next()
        self.assertEqual(heartbeat, {"op": 1, "d": 1})
        socket.feed({"op": 11})
        second = await socket.next()
        self.assertEqual(second["op"], 1)
        socket.feed({"op": 11})
        socket.feed({"op": 1})
        self.assertEqual(await socket.next(), {"op": 1, "d": 1})
        socket.feed({"op": 11})
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task

    async def test_buffered_ack_does_not_interrupt_event_receipt(self):
        socket, task, _ = await self.session()
        socket.feed(ready())
        socket.feed({"op": 11})
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task

    async def test_session_cancellation_joins_heartbeat(self):
        socket, task, _ = await self.session(interval=20)
        socket.feed(ready())
        await socket.next()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        while not socket.outgoing.empty():
            socket.outgoing.get_nowait()
        await asyncio.sleep(0.04)
        self.assertTrue(socket.outgoing.empty())

    async def test_missing_heartbeat_ack_does_not_add_extra_timeout(self):
        socket, task, _ = await self.session(interval=20)
        socket.feed(ready())
        self.assertEqual((await socket.next())["op"], 1)
        self.assertEqual((await socket.next())["op"], 1)
        self.assertFalse(task.done())
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task
        self.assertEqual(self.gateway._session_id, "session-id")

    async def test_resume_replays_and_advances_cursor_before_handler_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def capture(context):
            entered.set()
            await release.wait()

        self.app.router.message("capture", capture)
        socket, task, auth = await self.session(resume=True)
        self.assertEqual(auth, {"op": 6, "d": {"token": "QQBot fake-token", "session_id": "session-id", "seq": 5}})
        frame = payload()
        frame["s"] = 6
        socket.feed(frame)
        await asyncio.wait_for(entered.wait(), 1)
        # 序号代表最后收到的 Dispatch，不代表业务已完成。
        socket.feed({"op": 0, "t": "RESUMED", "s": 6, "d": ""})
        await asyncio.sleep(0.01)
        self.assertEqual(self.gateway._received, 6)
        release.set()
        await _drain(self.gateway._stream)
        self.assertEqual(self.gateway._received, 6)
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task

    async def test_invalid_session_clears_resume_state(self):
        socket, task, auth = await self.session(resume=True)
        socket.feed({"op": 9, "d": False})
        with self.assertRaises(GatewayReconnect):
            await task
        self.assertIsNone(self.gateway._session_id)
        self.assertIsNone(self.gateway._received)

    async def test_unknown_events_advance_processed_cursor(self):
        socket, task, _ = await self.session()
        socket.feed(ready())
        socket.feed({"op": 0, "s": 2, "t": "UNSUPPORTED_NOTICE", "d": {}})
        for _ in range(50):
            if self.gateway._received == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.gateway._received, 2)
        await _drain(self.gateway._stream)
        self.assertFalse(self.gateway._stream._tasks)
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task

    async def test_disconnect_keeps_received_events_running(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        completed = []

        async def capture(context):
            entered.set()
            await release.wait()
            completed.append(context.event.message_id)

        self.app.router.message("capture", capture)
        socket, task, _ = await self.session()
        socket.feed(ready())
        first = payload(message_id="started")
        first["s"] = 2
        socket.feed(first)
        await asyncio.wait_for(entered.wait(), 1)
        stream = self.gateway._stream
        second = payload(message_id="second")
        second["s"] = 3
        socket.feed(second)
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task
        await self.gateway._disconnect()
        self.assertEqual(self.gateway._received, 3)
        self.assertFalse(stream.closed)
        self.assertTrue(stream._tasks)
        self.assertFalse(self.app.bots)
        release.set()
        await _drain(stream)
        self.assertEqual(set(completed), {"started", "second"})

    async def test_resume_cursor_does_not_wait_for_slow_prefix(self):
        entered, release, later_processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def capture(context):
            if context.event.message_id == "slow":
                entered.set()
                await release.wait()
            else:
                later_processed.set()

        self.app.router.message("capture", capture)
        socket, task, _ = await self.session()
        socket.feed(ready())
        first, second = payload(message_id="slow"), payload(message_id="fast")
        first["s"], second["s"] = 2, 3
        socket.feed(first)
        await asyncio.wait_for(entered.wait(), 1)
        socket.feed(second)
        await asyncio.wait_for(later_processed.wait(), 1)
        self.assertEqual(self.gateway._received, 3)
        release.set()
        await asyncio.wait_for(_drain(self.gateway._stream), 1)
        self.assertEqual(self.gateway._received, 3)
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task

    async def test_heartbeats_continue_while_handler_is_blocked(self):
        entered = asyncio.Event()

        async def capture(context):
            entered.set()
            await asyncio.Event().wait()

        self.app.router.message("capture", capture)
        socket, task, _ = await self.session(interval=30)
        socket.feed(ready())
        frame = payload()
        frame["s"] = 2
        socket.feed(frame)
        await asyncio.wait_for(entered.wait(), 1)
        heartbeat = await socket.next()
        self.assertEqual(heartbeat["op"], 1)
        self.assertIn(heartbeat["d"], {1, 2})
        self.assertEqual(self.gateway._received, 2)
        socket.feed({"op": 11})
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task

    async def test_invalid_hello_is_protocol_error_not_fallback(self):
        socket = Socket()
        socket.feed({"op": 7})
        with self.assertRaises(GatewayProtocolError):
            await self.gateway.session(socket)
        self.assertTrue(socket.outgoing.empty())

    async def test_missing_dispatch_sequence_keeps_sequence_optional(self):
        socket, task, _ = await self.session()
        socket.feed(ready())
        socket.feed(payload())
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task
        self.assertIsNone(self.gateway._received)

    async def test_gateway_discovery_does_not_add_url_restrictions(self):
        for info in (
            gateway_info(url="ws://gateway.test"),
            gateway_info(url="wss://secret@gateway.test"),
        ):
            self.gateway.api.request.return_value = info
            self.assertEqual(await self.gateway._gateway(), info["url"])

    async def test_explicit_shard_and_zero_identify_quota(self):
        self.gateway.config = replace(self.config, shard=(1, 2))
        self.gateway.api.request.return_value = gateway_info(shards=2)
        await self.gateway._gateway()
        self.assertEqual(self.gateway._shard, (1, 2))
        info = gateway_info()
        info["session_start_limit"]["remaining"] = 0
        self.gateway.api.request.return_value = info
        self.assertEqual(await self.gateway._gateway(), gateway_info()["url"])

    async def test_resume_reuses_original_http_client_and_reply_sequences(self):
        socket, task, _ = await self.session()
        socket.feed(ready())
        await self.wait_online()
        old_client = self.app.bots[0]
        old_client._sequence[("key", "message")] = (2, 9999999999)
        socket.feed({"op": 7})
        with self.assertRaises(GatewayReconnect):
            await task
        await self.gateway._disconnect()
        old_client._online()
        self.assertFalse(self.app.bots)
        self.gateway._activate()
        self.assertIs(self.app.bots[0], old_client)
        self.assertEqual(self.app.bots[0]._sequence[("key", "message")][0], 2)
        await self.gateway.close()
        with self.assertRaises(BotOffline):
            old_client._online()

    async def test_intents_close_is_logged_and_reconnected_like_protocol(self):
        attempts = []

        @asynccontextmanager
        async def connector(url, **kwargs):
            attempts.append(url)
            socket = Socket()
            socket.feed({"op": 10, "d": {"heartbeat_interval": 1000}})
            socket.feed(ConnectionClosedError(Close(4014, "not allowed"), None))
            if len(attempts) == 2:
                self.gateway._stopped = True
            self.assertIsNone(kwargs["proxy"])
            self.assertIsNone(kwargs["ping_interval"])
            yield socket

        self.gateway._connector = connector
        self.gateway._pause = AsyncMock()
        await self.gateway.run()
        self.assertEqual(len(attempts), 2)
        self.assertEqual([call.args for call in self.gateway._pause.await_args_list], [(3.0,), (3.0,)])
        await self.gateway.close()
        self.assertTrue(self.gateway.api._http.is_closed)
        self.assertFalse(self.app.bots)

    async def test_invalid_sequence_close_identifies_again(self):
        attempts, auth = [], []

        @asynccontextmanager
        async def connector(url, **kwargs):
            socket = Socket()
            socket.feed({"op": 10, "d": {"heartbeat_interval": 10000}})
            socket.feed(ConnectionClosedError(Close(4007, ""), None))
            old_send = socket.send

            async def send(data):
                auth.append(json.loads(data)["op"])
                await old_send(data)

            socket.send = send
            attempts.append(url)
            if len(attempts) == 2:
                self.gateway._stopped = True
            yield socket

        self.gateway._session_id, self.gateway._received = "old-session", 3
        self.gateway._connector = connector
        self.gateway._pause = AsyncMock()
        await self.gateway.run()
        self.assertEqual(auth, [6, 6])

    async def test_gateway_task_shutdown_interrupts_reconnect_wait(self):
        waiting = asyncio.Event()

        async def pause(seconds):
            waiting.set()
            await asyncio.Event().wait()

        self.gateway._pause = pause

        @asynccontextmanager
        async def connector(url, **kwargs):
            raise OSError("fake")
            yield

        self.gateway._connector = connector
        self.gateway.start()
        await asyncio.wait_for(waiting.wait(), 1)
        await self.gateway.close()
        self.assertTrue(self.gateway._runner.done())
        self.assertTrue(self.gateway.api._http.is_closed)
        self.assertFalse(self.app.bots)

    async def test_reconnect_resumes_existing_session_with_new_client(self):
        sockets = [Socket(), Socket()]
        for socket in sockets:
            socket.feed({"op": 10, "d": {"heartbeat_interval": 10000}})
        sockets[0].feed(ready())
        sockets[0].feed({"op": 7})
        sockets[1].feed({"op": 0, "t": "RESUMED", "s": 2, "d": ""})
        connections = []

        @asynccontextmanager
        async def connector(url, **kwargs):
            index = len(connections)
            connections.append(url)
            yield sockets[index]

        self.gateway._connector = connector
        self.gateway._pause = AsyncMock()
        self.gateway.start()
        try:
            first = await sockets[0].next()
            resumed = await sockets[1].next()
            self.assertEqual(first["op"], 2)
            self.assertEqual(
                resumed,
                {
                    "op": 6,
                    "d": {
                        "token": "QQBot fake-token",
                        "session_id": "session-id",
                        "seq": 1,
                    },
                },
            )
            for _ in range(50):
                if self.gateway._received == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual(self.gateway._received, 2)
            self.assertEqual(len(connections), 2)
            self.assertEqual(len(self.app.bots), 1)
        finally:
            await self.gateway.close()


def parse_target():
    from fxbot.core import SessionTarget

    return SessionTarget("qq", APP_ID, "group", "GROUP_OPENID")


class QQGatewayLoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_loopback_websocket_identify_ping_heartbeat_and_shutdown(self):
        app = Application()
        await app.start()
        sent = []
        heartbeat_seen = asyncio.Event()
        replied = asyncio.Event()

        async def server(socket):
            await socket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30}}))
            auth = json.loads(await socket.recv())
            self.assertEqual(auth["op"], 2)
            self.assertEqual(auth["d"]["token"], "QQBot fake-token")
            await socket.send(json.dumps(ready()))
            frame = payload(content="/ping")
            frame["s"] = 2
            await socket.send(json.dumps(frame))
            async for raw in socket:
                packet = json.loads(raw)
                if packet["op"] == 1:
                    heartbeat_seen.set()
                    await socket.send('{"op":11}')

        async with serve(server, "127.0.0.1", 0, ping_interval=None) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]

            def connector(url, **kwargs):
                self.assertTrue(url.startswith("wss://"))
                self.assertIsNone(kwargs["proxy"])
                return connect(f"ws://127.0.0.1:{port}", **kwargs)

            config = QQBotSettings(SECRET, mode="websocket")
            settings = Settings("test", qq_bots={APP_ID: config})
            gateway = QQGateway(app, APP_ID, config, settings, connector=connector)
            await gateway.api._http.aclose()

            def http(request):
                if str(request.url) == AUTH_URL:
                    return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
                if request.url.path == "/gateway/bot":
                    return httpx.Response(200, json=gateway_info())
                sent.append(request)
                replied.set()
                return httpx.Response(200, json={"id": "sent-message"})

            gateway.api._http = httpx.AsyncClient(transport=httpx.MockTransport(http))

            async def ping(context):
                await context.send("pong")

            app.router.command("ping", "/ping", ping)
            gateway.start()
            try:
                await asyncio.wait_for(replied.wait(), 3)
                await asyncio.wait_for(heartbeat_seen.wait(), 3)
                self.assertEqual(sent[0].url.path, "/v2/groups/GROUP_OPENID/messages")
                self.assertEqual(json.loads(sent[0].content)["msg_seq"], 1)
                self.assertEqual(json.loads(sent[0].content)["msg_id"], "msg=1")
            finally:
                await gateway.close()
                await app.stop()
            self.assertFalse(app.bots)
            self.assertTrue(gateway.api._http.is_closed)
            self.assertTrue(gateway._runner.done())


class QQGatewayConfigTests(unittest.TestCase):
    def test_default_intents_target_group_and_c2c_not_channels(self):
        config = QQBotSettings(SECRET)
        self.assertEqual(config.intents, (1 << 24) | (1 << 25) | (1 << 26) | (1 << 27))
        self.assertFalse(config.intents & ((1 << 0) | (1 << 1) | (1 << 10) | (1 << 30)))

    def test_mode_intents_and_shard_config(self):
        config = QQBotSettings.parse({"secret": SECRET, "mode": "websocket", "intents": 1 << 25, "shard": [0, 2]})
        self.assertEqual(config.mode, "websocket")
        self.assertEqual(config.shard, (0, 2))
        for raw in (
            {"secret": SECRET, "mode": "both"},
            {"secret": SECRET, "intents": True},
            {"secret": SECRET, "shard": [0]},
        ):
            with self.assertRaises(ValueError):
                QQBotSettings.parse(raw)

    def test_invalid_frames_not_guessed(self):
        for raw in ("[]", "{broken"):
            with self.assertRaises(GatewayProtocolError):
                decode_frame(raw)


class QQGatewayModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_mode_starts_gateway_and_accepts_signed_webhook(self):
        from starlette.requests import Request

        from tests.runtime.test_qq import signed

        app = Application()
        await app.start()
        config = QQBotSettings(SECRET, mode="websocket")
        settings = Settings("test", qq_bots={APP_ID: config})
        service = QQService(app, settings)
        waiting = asyncio.Event()

        async def run(gateway):
            waiting.set()
            await asyncio.Event().wait()

        try:
            with patch.object(QQGateway, "run", run):
                await service.start()
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertIsInstance(service.connections[APP_ID], QQGateway)
                gateway = service.connections[APP_ID]
                gateway._bot_user_id = "BOT_USER"
                raw, headers = signed(payload())

                async def receive():
                    return {"type": "http.request", "body": raw}

                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
                    },
                    receive,
                )
                self.assertEqual((await service.handle(request)).status_code, 200)
                self.assertEqual(len(app.bots), 1)
        finally:
            gateway = service.connections[APP_ID]
            await service.close()
            await app.stop()
            self.assertTrue(gateway._runner.done())
            self.assertTrue(gateway.api._http.is_closed)
