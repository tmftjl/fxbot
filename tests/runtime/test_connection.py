"""协议动作关联、异常退出和连接回收测试。"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from fxbot.core.errors import BotOffline, ActionFailed
from fxbot.core.application import Application
from fxbot.runtime.settings import Settings
from fxbot.runtime.connection import ProtocolConnection
from tests.migration.test_onebot11 import payload
from fxbot.platforms.onebot11.protocol import OneBot11Protocol


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    """使用可控传输函数验证网络边界，不访问外部平台。"""

    async def asyncSetUp(self) -> None:
        self.application = Application()
        await self.application.start()
        self.sent = asyncio.Queue()
        self.close_transport = AsyncMock()
        self.connection = ProtocolConnection(
            self.application,
            OneBot11Protocol(),
            "100",
            Settings("test-only", action_timeout=0.2),
            self.sent.put,
            self.close_transport,
        )
        self.connection.start()
        self.addAsyncCleanup(self.application.stop)

    async def next_action(self) -> dict:
        """等待一次出站动作，测试失败时不会无限挂起。"""
        return await asyncio.wait_for(self.sent.get(), 1)

    async def test_connection_close_joins_an_already_cancelled_consumers_cleanup(self) -> None:
        entered, cleaning, release, cleaned, interrupted = (asyncio.Event() for _ in range(5))
        stopping_consumer = asyncio.Event()

        async def handler(context) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
                cleaned.set()

        original_stop = self.connection._stop_consumer

        async def stop_consumer() -> None:
            stopping_consumer.set()
            await original_stop()

        self.connection._stop_consumer = stop_consumer
        self.application.router.message("cleanup", handler)
        self.connection.receive(payload())
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.application.cancel(self.connection._worker)
            await asyncio.wait_for(cleaning.wait(), 1)
            closing = asyncio.create_task(self.connection.close())
            await asyncio.wait_for(stopping_consumer.wait(), 1)
            await asyncio.sleep(0)
            self.assertFalse(interrupted.is_set())
            self.assertFalse(closing.done())
            release.set()
            await asyncio.wait_for(closing, 1)
            self.assertTrue(cleaned.is_set())
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 1)

    def respond(self, action: dict, data: object, *, ok: bool = True) -> None:
        """发送对应动作的成功或失败响应。"""
        self.connection.receive(
            {
                "echo": action["echo"],
                "data": data,
                "status": "ok" if ok else "failed",
                "retcode": 0 if ok else 1,
            }
        )

    async def test_out_of_order_responses_are_matched_by_echo(self) -> None:
        first = asyncio.create_task(self.connection.client.call("first"))
        second = asyncio.create_task(self.connection.client.call("second"))
        actions = {action["action"]: action for action in (await self.next_action(), await self.next_action())}
        self.respond(actions["second"], {"value": 2})
        self.respond(actions["first"], {"value": 1})
        self.assertEqual(await first, {"value": 1})
        self.assertEqual(await second, {"value": 2})
        self.assertEqual(self.connection.actions.pending_count, 0)

    async def test_timeout_cleans_pending_and_late_response_is_ignored(self) -> None:
        call = asyncio.create_task(self.connection.client.call("slow"))
        action = await self.next_action()
        with self.assertRaises(asyncio.TimeoutError):
            await call
        self.respond(action, {"late": True})
        self.assertEqual(self.connection.actions.pending_count, 0)
        self.assertTrue(self.sent.empty())

    async def test_failed_action_does_not_leak_platform_error(self) -> None:
        call = asyncio.create_task(self.connection.client.call("failed"))
        action = await self.next_action()
        self.respond(action, {"message": "secret upstream error"}, ok=False)
        with self.assertRaises(ActionFailed) as caught:
            await call
        self.assertNotIn("secret", str(caught.exception))

    async def test_caller_cancellation_removes_pending_request(self) -> None:
        call = asyncio.create_task(self.connection.client.call("cancelled"))
        action = await self.next_action()
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.respond(action, {})
        self.assertEqual(self.connection.actions.pending_count, 0)

    async def test_disconnect_ends_pending_calls_and_old_client_is_offline(self) -> None:
        call = asyncio.create_task(self.connection.client.call("pending"))
        await self.next_action()
        await self.connection.close()
        with self.assertRaises(BotOffline):
            await call
        with self.assertRaises(BotOffline):
            await self.connection.client.call("offline")
        self.assertEqual(self.application.bots, ())
        self.assertEqual(self.connection.actions.pending_count, 0)
        self.close_transport.assert_awaited_once_with(1001)

    async def test_disconnect_cancels_blocked_send_without_waiting_for_action_timeout(self) -> None:
        entered = asyncio.Event()

        async def send(data: dict) -> None:
            entered.set()
            await asyncio.Event().wait()

        await self.connection.close()
        self.connection = ProtocolConnection(
            self.application,
            OneBot11Protocol(),
            "100",
            Settings("test-only", action_timeout=60),
            send,
            self.close_transport,
        )
        self.connection.start()
        call = asyncio.create_task(self.connection.client.call("blocked"))
        await entered.wait()
        await asyncio.wait_for(self.connection.close(), 1)
        with self.assertRaises(BotOffline):
            await asyncio.wait_for(call, 1)

    async def test_close_is_shared_and_survives_cancelled_waiter(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def close(code: int) -> None:
            entered.set()
            await release.wait()

        self.close_transport.side_effect = close
        first = asyncio.create_task(self.connection.close())
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(self.connection.close())
        release.set()
        await second
        self.close_transport.assert_awaited_once()

    async def test_application_shutdown_closes_transport(self) -> None:
        await self.application.stop()
        self.close_transport.assert_awaited_once_with(1001)
        self.assertTrue(self.connection._worker.done())
        with self.assertRaises(BotOffline):
            await self.connection.client.call("after_shutdown")

    async def test_unregistered_connection_cannot_send(self) -> None:
        send = AsyncMock()
        connection = ProtocolConnection(
            self.application,
            OneBot11Protocol(),
            "200",
            Settings("test-only"),
            send,
            AsyncMock(),
        )
        with self.assertRaises(BotOffline):
            await connection.client.call("before_start")
        send.assert_not_awaited()
        await connection.close()

    async def test_pending_limit_rejects_without_sending(self) -> None:
        await self.connection.close()
        self.connection = ProtocolConnection(
            self.application,
            OneBot11Protocol(),
            "100",
            Settings("test-only", max_pending=1),
            self.sent.put,
            self.close_transport,
        )
        self.connection.start()
        first = asyncio.create_task(self.connection.client.call("pending"))
        action = await self.next_action()
        with self.assertRaises(ActionFailed):
            await self.connection.client.call("excess")
        self.respond(action, {})
        await first
        self.assertTrue(self.sent.empty())

    async def test_queue_overload_never_blocks_action_responses(self) -> None:
        self.connection._queue = asyncio.Queue(maxsize=1)
        self.connection.receive(payload())
        with self.assertRaises(asyncio.QueueFull):
            self.connection.receive(payload())
        await self.connection.close()
        await self.connection._queue.join()

    async def test_transport_failure_does_not_retry(self) -> None:
        await self.connection.close()
        send = AsyncMock(side_effect=OSError("send failed"))
        self.connection = ProtocolConnection(
            self.application,
            OneBot11Protocol(),
            "100",
            Settings("test-only"),
            send,
            self.close_transport,
        )
        self.connection.start()
        with self.assertRaises(OSError):
            await self.connection.client.call("failed")
        send.assert_awaited_once()
        self.assertEqual(self.connection.actions.pending_count, 0)

    async def test_close_timeout_is_reported_and_local_state_is_released(self) -> None:
        await self.connection.close()
        self.connection = ProtocolConnection(
            self.application,
            OneBot11Protocol(),
            "100",
            Settings("test-only", close_timeout=0.01),
            self.sent.put,
            self.close_transport,
        )
        self.connection.start()

        async def close(code: int) -> None:
            await asyncio.Event().wait()

        self.close_transport.side_effect = close
        with self.assertRaises(asyncio.TimeoutError):
            await self.connection.close()
        self.assertEqual(self.application.bots, ())
        self.assertTrue(self.connection._worker.done())


class SettingsTests(unittest.TestCase):
    """拒绝无效身份和无界时间设置。"""

    def test_invalid_numeric_settings_raise_value_error(self) -> None:
        for value in (True, 0, -1, float("inf"), float("nan"), "15", None):
            for name in ("action_timeout", "close_timeout"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    Settings("test-only", **{name: value})

    def test_token_is_optional_but_must_be_a_string(self) -> None:
        self.assertEqual(Settings().token, "")
        for token in (None, True, 123, " ", " secret "):
            with self.subTest(token=token), self.assertRaises(ValueError):
                Settings(token)
