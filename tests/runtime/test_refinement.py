"""审查问题回归：严格权限、单次取消、QQ HTTP 边界与已加载插件扩展。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from fxbot.core import Event, Message, SessionTarget
from fxbot.core.errors import BotOffline
from fxbot.core.context import bind_event, reset_event
from fxbot.runtime.actions import ActionBroker
from fxbot.permission.types import perm_entry_default
from fxbot.platforms.qq.api import QQHTTP, QQAPIError
from fxbot.permission.storage import PermissionStorage
from fxbot.platforms.qq.client import QQClient
from fxbot.permission.validation import validate_permissions


class PermissionContractTests(unittest.TestCase):
    def document(self):
        return {"top": perm_entry_default(), "sub_plugins": {}}

    def test_unknown_permission_values_and_malformed_scopes_are_rejected(self):
        for key, value in (
            ("level", "superusr"),
            ("scene", "privte"),
            ("enabled", "false"),
            ("blacklist", []),
            ("whitlist", {}),
        ):
            data = self.document()
            data["top"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_permissions(data)

    def test_invalid_save_preserves_original_file(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = PermissionStorage()
            storage._path = Path(directory) / "permissions.json"
            storage._cache_key = ("audit-permissions", directory)
            storage.save(self.document())
            original = storage.path.read_bytes()
            data = self.document()
            data["top"]["level"] = "superusr"
            with self.assertRaises(ValueError):
                storage.save(data)
            self.assertEqual(storage.path.read_bytes(), original)

    def test_non_object_permissions_are_not_replaced_by_empty_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = PermissionStorage()
            storage._path = Path(directory) / "permissions.json"
            storage._path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                storage._read()


class ActionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_background_task_does_not_reuse_expired_passive_message_id(self):
        api = SimpleNamespace(request=AsyncMock(return_value={"id": "sent"}))
        bot = QQClient("123", api)
        target = SessionTarget("qq", "123", "private", "user")
        event = Event(None, "expired", "message", "qq", "123", "user", "", target, Message.text("hello"))
        release = asyncio.Event()
        token = bind_event(bot, event)

        async def child():
            await release.wait()
            await bot.send(target, Message.text("background"))

        task = asyncio.create_task(child())
        reset_event(token)
        release.set()
        await task
        body = api.request.await_args.kwargs["json"]
        self.assertNotIn("msg_id", body)
        self.assertNotIn("event_id", body)

    async def test_close_and_call_share_one_cancel_request_and_wait_for_cleanup(self):
        entered, cleaning, release, interrupted = (asyncio.Event() for _ in range(4))
        self.addCleanup(release.set)

        async def send(packet):
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

        broker = ActionBroker(send, timeout=30, capacity=1)
        request = asyncio.create_task(broker.call("test", {}))
        await entered.wait()
        close = asyncio.create_task(broker.aclose())
        await cleaning.wait()
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(interrupted.is_set())
        self.assertFalse(close.done())
        release.set()
        await close
        with self.assertRaises(BotOffline):
            await request


class QQResponseBoundaryTests(unittest.TestCase):
    def test_non_json_http_failure_keeps_status_for_gateway_retry_policy(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status), self.assertRaises(QQAPIError) as caught:
                QQHTTP._response(httpx.Response(status, text="<html>upstream unavailable</html>"))
            self.assertEqual(caught.exception.status, status)

    def test_malformed_success_is_not_treated_as_transient_http_error(self):
        for response in (
            httpx.Response(200, text="not JSON"),
            httpx.Response(201, content=b"\xff"),
            httpx.Response(200, json=[]),
        ):
            with self.subTest(status=response.status_code), self.assertRaises(QQAPIError) as caught:
                QQHTTP._response(response)
            self.assertEqual(caught.exception.status, response.status_code)
            self.assertIsNone(caught.exception.code)


class MemoryQueryProjectionTests(unittest.TestCase):
    def test_timestamp_and_optional_number_keep_their_distinct_contracts(self):
        from fxbot.console.queries.memory import optional_float, format_timestamp

        self.assertIsNone(format_timestamp("invalid"))
        self.assertIsNone(format_timestamp(0))
        self.assertIsNotNone(format_timestamp(1700000000))
        self.assertIsNone(optional_float("invalid"))
        self.assertEqual(optional_float("0.25"), 0.25)

    def test_invalid_required_confidence_is_not_silently_converted_to_none(self):
        from fxbot.console.queries.memory import relation_view

        with self.assertRaises(ValueError):
            relation_view({"confidence": "invalid"}, "tenant", {})
