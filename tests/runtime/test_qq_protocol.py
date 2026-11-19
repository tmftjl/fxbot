"""QQ 官方 API 请求与事件字段契约测试，只读取本地快照，不访问真实服务。"""

from __future__ import annotations

import json
import types
import typing
import asyncio
import inspect
import unittest
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import BaseModel

import fxbot.platforms.qq.actions as actions
from fxbot.platforms.qq.api import QQHTTP, QQAPIError, QQAuditPending
from fxbot.platforms.qq.audit import audit_result
from fxbot.platforms.qq.codec import api_path, parse_event, parse_message
from fxbot.platforms.qq.actions import QQActions
from fxbot.platforms.qq.event_models import EVENT_CLASSES

SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "qq_api_snapshot.json"


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 19, 10, tzinfo=tz)


def sample(annotation, variant):
    if annotation is type(None):
        return None
    origin, arguments = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (types.UnionType, typing.Union):
        arguments = [value for value in arguments if value is not type(None)]
        if variant == 2:
            arguments.sort(key=lambda value: not (inspect.isclass(value) and issubclass(value, (datetime, BaseModel))))
        return sample(arguments[0], variant)
    if origin is typing.Literal:
        return arguments[0]
    if origin is list:
        return [sample(arguments[0], variant)]
    if origin is tuple:
        return tuple(sample(argument, variant) for argument in arguments)
    if origin is dict or annotation is dict:
        return {"key": "value"}
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation(
            **{
                key: sample(field.annotation, variant)
                for key, field in annotation.model_fields.items()
                if field.is_required()
            }
        )
    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        return next(iter(annotation))
    if inspect.isclass(annotation) and issubclass(annotation, datetime):
        return FrozenDatetime.now(timezone.utc)
    return {str: "value", bytes: b"bytes", bool: True, int: 1, float: 1.5, timedelta: timedelta(seconds=10)}[annotation]


def arguments(method, variant):
    hints = typing.get_type_hints(method)
    return {
        name: sample(hints[name], variant)
        for name, parameter in inspect.signature(method).parameters.items()
        if parameter.default is inspect.Parameter.empty or variant
    }


def serializable(value):
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


class QQProtocolRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_special_ids_use_protocol_url_encoding_without_local_validation(self):
        cases = (
            ("", "/v2/groups/messages"),
            ("a/b", "/v2/groups/a/b/messages"),
            ("a?b#c", "/v2/groups/a%3Fb%23c/messages"),
            ("a b", "/v2/groups/a%20b/messages"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(api_path("v2", "groups", value, "messages"), expected)
        request = AsyncMock(return_value=[])
        await QQActions(request, AsyncMock()).get_schedules(channel_id="x?y#z")
        self.assertEqual(request.await_args.args, ("GET", "/channels/x%3Fy%23z/schedules"))

    def test_protocol_markup_parser(self):
        cases = (
            ("hello &lt;literal&gt; &quot;", [("text", {"text": "hello <literal> &quot;"})]),
            ("<@user>", [("mention", {"user_id": "user", "name": ""})]),
            ("<#channel>", [("qq:mention_channel", {"channel_id": "channel"})]),
            ("@everyone<qqbot-at-everyone />", []),
            ("<@all>", [("mention_all", {})]),
            ("<emoji:123>", []),
            ('<faceType=1,faceId="0",ext="x">', []),
            ('<faceType=1,faceId="123",ext="x">', [("qq:emoji", {"id": "123"})]),
            ("<@with-hyphen>", [("text", {"text": "<@with-hyphen>"})]),
        )
        for content, expected in cases:
            with self.subTest(content=content):
                actual = parse_message({"content": content})
                self.assertEqual([(part.type, part.data) for part in actual.segments], expected)

    async def test_401_refreshes_and_replays_the_same_write_once(self):
        auth, writes = [], []

        def response(request):
            if request.url.path.endswith("getAppAccessToken"):
                auth.append(request)
                return httpx.Response(200, json={"access_token": f"token-{len(auth)}", "expires_in": 3600})
            writes.append(request)
            return httpx.Response(401, text="expired") if len(writes) == 1 else httpx.Response(200, json={"id": "sent"})

        api = QQHTTP("app", "secret", httpx.AsyncClient(transport=httpx.MockTransport(response)))
        try:
            result = await api.request("POST", "/v2/groups/group/messages", json={"content": "hello", "msg_seq": 1})
            self.assertEqual(result, {"id": "sent"})
            self.assertEqual(len(auth), 2)
            self.assertEqual(len(writes), 2)
            self.assertEqual(writes[0].content, writes[1].content)
            self.assertEqual(writes[1].headers["Authorization"], "QQBot token-2")
        finally:
            await api.close()

    async def test_audit_pending_waits_for_result_and_releases_waiter(self):
        api = QQHTTP("app", "secret", httpx.AsyncClient())
        try:
            with self.assertRaises(QQAuditPending) as pending:
                api._response(httpx.Response(202, json={"data": {"message_audit": {"audit_id": "audit"}}}))
            waiter = asyncio.create_task(pending.exception.get_audit_result(1))
            await asyncio.sleep(0)
            result = parse_event(
                {
                    "op": 0,
                    "t": "MESSAGE_AUDIT_PASS",
                    "d": {
                        "audit_id": "audit",
                        "audit_time": "2026-11-19T10:00:00Z",
                        "channel_id": "channel",
                    },
                },
                bot_id="app",
            )
            audit_result.add_result(result)
            self.assertIs(await waiter, result)
            self.assertNotIn("audit", audit_result._futures)
            with self.assertRaises(asyncio.TimeoutError):
                await audit_result.fetch("missing", timeout=0.001)
            self.assertNotIn("missing", audit_result._futures)
            with self.assertRaises(QQAPIError):
                api._response(httpx.Response(201, json={}))
            self.assertEqual(api._response(httpx.Response(200, json=[{"id": "guild"}])), [{"id": "guild"}])
        finally:
            await api.close()

    def test_all_55_event_schemas_enter_core_without_restricting_ids(self):
        self.assertEqual(len(EVENT_CLASSES), 55)
        for name, model in EVENT_CLASSES.items():
            with self.subTest(event=name):
                data = sample(model, 0).model_dump(mode="json", exclude_none=True)
                event = parse_event({"op": 0, "t": name, "id": "event", "d": data}, bot_id="app")
                self.assertEqual(event.metadata["qq_event_type"], name)
                self.assertEqual(event.metadata["qq_data"], data)

    async def test_all_api_requests_match_snapshot_contracts(self):
        cases = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        calls = []

        async def record(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {}

        client = QQActions(record, AsyncMock())
        names = {case["method"] for case in cases}
        actual = {
            name
            for name, method in vars(QQActions).items()
            if inspect.iscoroutinefunction(method) and not name.startswith("_")
        }
        self.assertEqual(actual, names | {"put_upload_part", "post_c2c_upload", "post_group_upload"})
        with patch.object(actions, "datetime", FrozenDatetime):
            for case in cases:
                with self.subTest(method=case["method"], variant=case["variant"]):
                    calls.clear()
                    method = getattr(client, case["method"])
                    error = None
                    try:
                        await method(**arguments(method, case["variant"]))
                    except ValueError as caught:
                        error = str(caught)
                    self.assertEqual(serializable(calls), case["calls"])
                    self.assertEqual(error, case["error"])
