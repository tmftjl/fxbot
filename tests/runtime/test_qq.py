"""QQ 官方适配器：只使用已签名测试回调和 MockTransport，不连接真实服务。"""

from __future__ import annotations

import json
import time
import asyncio
import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from fxbot.core import Message, BotClient, Capability, SessionTarget, MessageSegment, UnsupportedCapability
from fxbot.core.context import bind_event, reset_event
from fxbot.core.application import Application
from fxbot.platforms.qq.api import QQHTTP, AUTH_URL, UPLOAD_READ_TIMEOUT, QQAPIError
from fxbot.runtime.settings import Settings
from fxbot.platforms.qq.codec import parse_event
from fxbot.platforms.qq.client import QQClient
from fxbot.platforms.qq.config import QQBotSettings
from fxbot.platforms.qq.signing import QQSigner, SignatureRejected
from fxbot.platforms.qq.webhook import QQWebhookConnection

SECRET = "local-test-secret"
APP_ID = "123456"


def payload(kind="GROUP_AT_MESSAGE_CREATE", message_id="msg=1", content="/ping"):
    author = {"id": "numeric-id-not-openid", "username": "昵称"}
    if kind.startswith("C2C"):
        author["user_openid"] = "USER_OPENID"
    elif kind.startswith("GROUP"):
        author["member_openid"] = "MEMBER_OPENID"
    else:
        author["id"] = "GUILD_USER"
    return {
        "op": 0,
        "t": kind,
        "id": kind + ":test",
        "d": {
            "id": message_id,
            "content": content,
            "author": author,
            "group_openid": "GROUP_OPENID",
            "channel_id": "CHANNEL_ID",
            "guild_id": "GUILD_ID",
            "timestamp": "2026-11-13T10:00:00+08:00",
        },
    }


def signed(data, secret=SECRET, app_id=APP_ID, *, timestamp=None):
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time())) if timestamp is None else timestamp
    signature = QQSigner(secret)._key.sign(timestamp.encode("ascii") + raw).hex()
    return raw, {
        "X-Bot-Appid": app_id,
        "X-Signature-Timestamp": timestamp,
        "X-Signature-Ed25519": signature,
        "Content-Type": "application/json",
    }


def settings(**kwargs):
    return Settings("test-only", qq_bots={APP_ID: QQBotSettings(SECRET)}, **kwargs)


class QQConfigTests(unittest.TestCase):
    def test_credentials_load_and_are_not_in_repr(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "token": "test-only",
                        "qq_bots": {APP_ID: {"secret": SECRET}},
                    }
                ),
                encoding="utf-8",
            )
            loaded = Settings.load(path)
            self.assertEqual(loaded.qq_bots[APP_ID].secret, SECRET)
            self.assertNotIn(SECRET, repr(loaded))
            self.assertNotIn(SECRET, repr(loaded.qq_bots[APP_ID]))
            with self.assertRaises(TypeError):
                loaded.qq_bots["other"] = QQBotSettings(SECRET)

    def test_invalid_credentials_or_config_rejected(self):
        for raw in ({APP_ID: SECRET}, {"other": QQBotSettings(SECRET)}, {123: QQBotSettings(SECRET)}):
            with self.assertRaises(ValueError):
                Settings("x", qq_bots=raw)
        for raw in ({"secret": ""}, {"secret": SECRET, "verify": False}, {"secret": SECRET, "bot_user_id": 1}):
            with self.assertRaises(ValueError):
                QQBotSettings.parse(raw)
        self.assertEqual(Settings(qq_bots={APP_ID: QQBotSettings(SECRET)}).qq_bots[APP_ID].secret, SECRET)


class QQCodecTests(unittest.TestCase):
    def test_group_uses_member_and_group_openids_not_old_ids(self):
        data = payload()
        event = parse_event(data, bot_id=APP_ID)
        self.assertEqual(event.platform, "qq")
        self.assertEqual(event.bot_id, APP_ID)
        self.assertEqual(event.user_id, "MEMBER_OPENID")
        self.assertEqual(event.target, SessionTarget("qq", APP_ID, "group", "GROUP_OPENID"))
        self.assertTrue(event.to_me)
        self.assertNotIn("sender_role", event.metadata)
        self.assertEqual(event.message_id, "msg=1")

    def test_c2c_and_dms_are_separate_namespaces(self):
        private = parse_event(payload("C2C_MESSAGE_CREATE"), bot_id=APP_ID)
        direct = parse_event(payload("DIRECT_MESSAGE_CREATE"), bot_id=APP_ID)
        self.assertEqual(private.target.target_id, "USER_OPENID")
        self.assertEqual(direct.target.target_id, "dms/GUILD_ID/GUILD_USER")
        self.assertNotEqual(private.target, direct.target)
        self.assertEqual(private.target.kind, "private")
        self.assertEqual(direct.target.kind, "private")

    def test_channel_bot_mention_does_not_confuse_appid_with_userid(self):
        event = parse_event(
            payload("AT_MESSAGE_CREATE", content="<@BOT_USER> /ping"), bot_id=APP_ID, bot_user_id="BOT_USER"
        )
        self.assertEqual(event.target.kind, "channel")
        self.assertEqual(event.message.plain_text(), "/ping")
        self.assertEqual(event.original_message.mentions(), ["BOT_USER"])
        self.assertEqual(event.metadata["guild_id"], "GUILD_ID")

    def test_original_message_is_independent_and_escaped_mentions_stay_text(self):
        source = payload(content="&lt;@USER&gt; &amp; 测试")
        original = deepcopy(source)
        event = parse_event(source, bot_id=APP_ID)
        self.assertEqual(event.message.mentions(), [])
        self.assertEqual(event.message.plain_text(), "<@USER> & 测试")
        event.message.segments[0].data["text"] = "changed"
        self.assertNotEqual(event.message.plain_text(), event.original_message.plain_text())
        self.assertEqual(source, original)

    def test_media_and_reference_are_normalized(self):
        source = payload()
        source["d"].update(
            attachments=[{"content_type": "image/png", "url": "example.com/image.png"}],
            message_reference={"message_id": "ref=/id"},
        )
        event = parse_event(source, bot_id=APP_ID)
        self.assertEqual(event.message.segments[-1].type, "image")
        self.assertEqual(event.message.segments[-1].data["file"], "https://example.com/image.png")
        self.assertEqual(event.reply.message_id, "ref=/id")

    def test_invalid_timestamp_role_or_identity_does_not_pass(self):
        for field, value in (("timestamp", "2026-11-13T10:00:00"), ("group_openid", "../x")):
            source = payload()
            source["d"][field] = value
            with self.assertRaises(ValueError):
                parse_event(source, bot_id=APP_ID)
        source = payload()
        source["d"]["author"]["member_role"] = "unknown"
        with self.assertRaises(ValueError):
            parse_event(source, bot_id=APP_ID)
        source["d"]["author"]["member_role"] = "admin"
        self.assertEqual(parse_event(source, bot_id=APP_ID).metadata["sender_role"], "admin")
        source["d"]["author"]["bot"] = True
        with self.assertRaises(UnsupportedCapability):
            parse_event(source, bot_id=APP_ID)

    def test_unknown_event_does_not_become_a_message(self):
        source = payload()
        source["t"] = "NEW_UNKNOWN_EVENT"
        with self.assertRaises(UnsupportedCapability):
            parse_event(source, bot_id=APP_ID)

    def test_group_mentions_use_signed_openid_mapping(self):
        source = payload("GROUP_MESSAGE_CREATE", content="<@internal-bot> <@internal-user> 好")
        source["d"]["mentions"] = [
            {"scope": "single", "id": "internal-bot", "is_you": True, "member_openid": "BOT_MEMBER"},
            {"scope": "single", "id": "internal-user", "is_you": False, "member_openid": "USER_MEMBER"},
        ]
        event = parse_event(source, bot_id=APP_ID)
        self.assertTrue(event.to_me)
        self.assertEqual(event.message.mentions(), ["USER_MEMBER"])
        self.assertEqual(event.original_message.mentions(), ["internal-bot", "internal-user"])


class QQSigningTests(unittest.TestCase):
    def test_signatures_bind_timestamp_body_and_bot_secret(self):
        raw, headers = signed(payload())
        signer = QQSigner(SECRET)
        signer.verify(raw, headers["X-Signature-Timestamp"], headers["X-Signature-Ed25519"])
        for body, timestamp, signature in (
            (raw + b" ", headers["X-Signature-Timestamp"], headers["X-Signature-Ed25519"]),
            (raw, "1", headers["X-Signature-Ed25519"]),
            (raw, headers["X-Signature-Timestamp"], "xyz"),
        ):
            with self.assertRaises(SignatureRejected):
                signer.verify(body, timestamp, signature)
        with self.assertRaises(SignatureRejected):
            QQSigner("other-secret").verify(raw, headers["X-Signature-Timestamp"], headers["X-Signature-Ed25519"])

    def test_handshake_uses_reference_seed_and_signs_exact_challenge(self):
        timestamp = str(int(time.time()))
        signer = QQSigner(SECRET)
        result = signer.challenge({"plain_token": "你好token", "event_ts": timestamp})
        signer._key.public_key().verify(bytes.fromhex(result["signature"]), (timestamp + "你好token").encode())
        self.assertEqual(result["plain_token"], "你好token")

    def test_handshake_cannot_be_used_to_sign_a_forged_dispatch(self):
        with self.assertRaises(ValueError):
            QQSigner(SECRET).challenge(
                {
                    "plain_token": json.dumps(payload()),
                    "event_ts": str(int(time.time())),
                }
            )


class QQHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_json_success_is_action_failure_without_retry_or_body_disclosure(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(200, text="<html>" + SECRET + "</html>")

        api = QQHTTP(APP_ID, SECRET, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        api.token = AsyncMock(return_value="fake-token")
        try:
            with self.assertRaises(QQAPIError) as caught:
                await api.request("POST", "/v2/groups/GROUP/messages", json={"content": "hello"})
            self.assertEqual(caught.exception.status, 200)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(SECRET, str(caught.exception))
        finally:
            await api.close()

    async def test_auth_token_shared_across_concurrent_calls_and_refreshed(self):
        calls = []

        def handle(request):
            calls.append(request)
            if str(request.url) == AUTH_URL:
                self.assertEqual(json.loads(request.content), {"appId": APP_ID, "clientSecret": SECRET})
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": "3600"})
            self.assertEqual(request.headers["Authorization"], "QQBot fake-token")
            self.assertEqual(request.headers["X-Union-Appid"], APP_ID)
            return httpx.Response(200, json={"id": "fake"})

        api = QQHTTP(APP_ID, SECRET, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        await asyncio.gather(api.request("GET", "/users/@me"), api.request("GET", "/users/@me"))
        self.assertEqual(sum(str(r.url) == AUTH_URL for r in calls), 1)
        api._expires_at = 0
        await api.token()
        self.assertEqual(sum(str(r.url) == AUTH_URL for r in calls), 2)
        await api.close()
        with self.assertRaises(ConnectionError):
            await api.token()

    async def test_write_failures_are_not_retried_and_errors_hide_response(self):
        writes = []

        def handle(request):
            if str(request.url) == AUTH_URL:
                return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
            writes.append(request)
            return httpx.Response(429, json={"code": 123, "message": SECRET})

        api = QQHTTP(APP_ID, SECRET, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        try:
            with self.assertRaises(QQAPIError) as caught:
                await api.request("POST", "/v2/groups/group/messages", json={"content": "x"})
            self.assertEqual(len(writes), 1)
            self.assertNotIn(SECRET, str(caught.exception))
        finally:
            await api.close()

    async def test_authentication_rejection_invalidates_for_next_call_without_retry(self):
        auth_calls = []

        def handle(request):
            if str(request.url) == AUTH_URL:
                auth_calls.append(request)
                return httpx.Response(200, json={"access_token": f"token-{len(auth_calls)}", "expires_in": "3600"})
            return httpx.Response(401, json={"code": 1})

        api = QQHTTP(APP_ID, SECRET, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        try:
            with self.assertRaises(QQAPIError):
                await api.request("POST", "/v2/groups/group/messages")
            self.assertEqual(len(auth_calls), 1)
            self.assertEqual(await api.token(), "token-2")
        finally:
            await api.close()

    async def test_upload_requests_match_original_unbounded_write_timeout(self):
        fake = SimpleNamespace(
            timeout=httpx.Timeout(15.0),
            request=AsyncMock(return_value=httpx.Response(200, json={})),
            put=AsyncMock(return_value=httpx.Response(200)),
            aclose=AsyncMock(),
        )
        api = QQHTTP(APP_ID, SECRET, fake)  # type: ignore[arg-type]
        api.token = AsyncMock(return_value="fake-token")
        try:
            await api.request("POST", "/v2/groups/GROUP/files", json={}, upload=True)
            timeout = fake.request.await_args.kwargs["timeout"]
            self.assertEqual(timeout.connect, 15.0)
            self.assertEqual(timeout.read, UPLOAD_READ_TIMEOUT)
            self.assertIsNone(timeout.write)
            await api.request("GET", "/users/@me")
            self.assertNotIn("timeout", fake.request.await_args.kwargs)
            await api.put_upload_part("https://upload.example.test/part", b"x")
            self.assertEqual(fake.put.await_args.kwargs["timeout"].read, UPLOAD_READ_TIMEOUT)
            self.assertIsNone(fake.put.await_args.kwargs["timeout"].write)
        finally:
            await api.close()

    async def test_error_message_is_reported_without_secrets(self):
        body = {"code": 40093007, "message": f"upload rejected {SECRET}", "trace_id": "trace-1"}

        api = QQHTTP(
            APP_ID,
            SECRET,
            httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400, json=body))),
        )
        api.token = AsyncMock(return_value="fake-token")
        try:
            with self.assertRaises(QQAPIError) as caught:
                await api.request("POST", "/v2/groups/GROUP/files", json={})
            text = str(caught.exception)
            self.assertEqual(caught.exception.code, 40093007)
            self.assertIn("upload rejected", text)
            self.assertIn("trace-1", text)
            self.assertNotIn(SECRET, text)
        finally:
            await api.close()

    async def test_missing_token_or_invalid_expiry_rejected(self):
        for body in (
            {"expires_in": 3600},
            {"access_token": "x", "expires_in": True},
            {"access_token": "x", "expires_in": -1},
        ):
            api = QQHTTP(
                APP_ID,
                SECRET,
                httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))),
            )
            try:
                with self.assertRaises(ValueError):
                    await api.token()
            finally:
                await api.close()


class QQClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = SimpleNamespace(request=AsyncMock(return_value={"id": "reply-id"}))
        self.bot = QQClient(APP_ID, self.api)
        self.event = parse_event(payload(), bot_id=APP_ID)

    def test_avatar_url_uses_qqapp_openid(self):
        self.assertIn(Capability.AVATAR, self.bot.capabilities)
        avatar = self.bot.capabilities.require(Capability.AVATAR)
        self.assertEqual(
            avatar.avatar_url("MEMBER_OPENID"),
            f"https://q.qlogo.cn/qqapp/{APP_ID}/MEMBER_OPENID/640",
        )

    async def test_botclient_protocol_and_unsupported_actions_make_no_request(self):
        self.assertIsInstance(self.bot, BotClient)
        with self.assertRaises(UnsupportedCapability):
            await self.bot.capabilities.require(Capability.MODERATION).ban("group", "user", 60)
        with self.assertRaises(UnsupportedCapability):
            await self.bot.capabilities.require(Capability.GROUP_DIRECTORY).get_groups()
        self.api.request.assert_not_awaited()

    async def test_passive_reply_uses_current_event_and_increments_msg_seq(self):
        token = bind_event(self.bot, self.event)
        try:
            await self.bot.send(self.event.target, Message.text("一"))
            await self.bot.send(self.event.target, Message.text("二"))
        finally:
            reset_event(token)
        calls = self.api.request.await_args_list
        self.assertEqual(calls[0].args, ("POST", "/v2/groups/GROUP_OPENID/messages"))
        self.assertEqual(calls[0].kwargs["json"]["msg_id"], "msg=1")
        self.assertEqual(calls[0].kwargs["json"]["msg_seq"], 1)
        self.assertEqual(calls[1].kwargs["json"]["msg_seq"], 2)

    async def test_passive_segment_supplies_msg_id_event_id_and_seq(self):
        message = Message([MessageSegment("qq:passive", {"msg_id": "ROBOT1.0_trigger"}), MessageSegment.text("一")])
        await self.bot.send(self.event.target, message)
        await self.bot.send(self.event.target, message)
        calls = self.api.request.await_args_list
        self.assertEqual(calls[0].kwargs["json"]["msg_id"], "ROBOT1.0_trigger")
        self.assertEqual(calls[0].kwargs["json"]["event_id"], "ROBOT1.0_trigger")
        self.assertEqual(calls[0].kwargs["json"]["msg_seq"], 1)
        self.assertEqual(calls[1].kwargs["json"]["msg_seq"], 2)

    async def test_passive_segment_rejects_duplicates(self):
        with self.assertRaises(ValueError):
            await self.bot.send(
                self.event.target,
                Message(
                    [
                        MessageSegment("qq:passive", {"msg_id": "first"}),
                        MessageSegment("qq:passive", {"msg_id": "second"}),
                    ]
                ),
            )
        self.api.request.assert_not_awaited()

    async def test_channel_passive_segment_sets_msg_id_without_seq(self):
        target = SessionTarget("qq", APP_ID, "channel", "CHANNEL")
        await self.bot.send(
            target,
            Message([MessageSegment("qq:passive", {"msg_id": "guild-msg"}), MessageSegment.text("x")]),
        )
        payload = self.api.request.await_args.kwargs["json"]
        self.assertEqual(payload["msg_id"], "guild-msg")
        self.assertNotIn("event_id", payload)
        self.assertNotIn("msg_seq", payload)

    async def test_media_upload_marks_request_for_longer_write(self):
        self.api.request.side_effect = [{"file_info": "media-info"}, {"id": "reply"}]
        await self.bot.send(self.event.target, Message([MessageSegment.image(b"PNG")]))
        upload, send = self.api.request.await_args_list
        self.assertIs(upload.kwargs["upload"], True)
        self.assertNotIn("upload", send.kwargs)

    async def test_slow_media_upload_does_not_block_other_targets(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(method, path, **kwargs):
            if path.endswith("/files"):
                started.set()
                await release.wait()
                return {"file_info": "media-info"}
            return {"id": f"id-{path}"}

        bot = QQClient(APP_ID, SimpleNamespace(request=request))
        first = asyncio.create_task(
            bot.send(SessionTarget("qq", APP_ID, "group", "GROUP_A"), Message([MessageSegment.image(b"PNG")]))
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        second = await asyncio.wait_for(
            bot.send(SessionTarget("qq", APP_ID, "group", "GROUP_B"), Message.text("hi")), timeout=1
        )
        self.assertTrue(second["message_id"].endswith("/messages"))
        release.set()
        await first

    async def test_timeout_does_not_reuse_sequence_or_retry(self):
        self.api.request.side_effect = [TimeoutError, {"id": "second"}]
        token = bind_event(self.bot, self.event)
        try:
            with self.assertRaises(TimeoutError):
                await self.bot.send(self.event.target, Message.text("一"))
            await self.bot.send(self.event.target, Message.text("二"))
        finally:
            reset_event(token)
        self.assertEqual(self.api.request.await_count, 2)
        self.assertEqual(self.api.request.await_args.kwargs["json"]["msg_seq"], 2)

    async def test_other_target_does_not_borrow_passive_reply_id(self):
        token = bind_event(self.bot, self.event)
        try:
            await self.bot.send(SessionTarget("qq", APP_ID, "group", "OTHER_GROUP"), Message.text("主动"))
        finally:
            reset_event(token)
        self.assertNotIn("msg_id", self.api.request.await_args.kwargs["json"])
        with self.assertRaises(ValueError):
            await self.bot.send(SessionTarget("qq", "other-bot", "group", "GROUP"), Message.text("no"))

    async def test_media_prevalidated_uploaded_once_then_sent(self):
        self.api.request.side_effect = [{"file_info": "media-info"}, {"id": "reply"}]
        await self.bot.send(self.event.target, Message([MessageSegment.text("图"), MessageSegment.image(b"PNG")]))
        upload, send = self.api.request.await_args_list
        self.assertEqual(upload.args[1], "/v2/groups/GROUP_OPENID/files")
        self.assertEqual(upload.kwargs["json"], {"file_type": 1, "srv_send_msg": False, "file_data": "UE5H"})
        self.assertEqual(send.kwargs["json"]["media"], {"file_info": "media-info"})
        self.assertEqual(send.kwargs["json"]["msg_type"], 7)

    async def test_http_remote_image_is_accepted(self):
        self.api.request.side_effect = [{"file_info": "media-info"}, {"id": "reply"}]
        await self.bot.send(self.event.target, Message([MessageSegment.image("http://gchat.qpic.cn/a.jpg")]))
        upload, send = self.api.request.await_args_list
        self.assertEqual(upload.kwargs["json"]["url"], "http://gchat.qpic.cn/a.jpg")
        self.assertEqual(send.kwargs["json"]["media"], {"file_info": "media-info"})

    async def test_invalid_media_no_partial_upload_or_fallback(self):
        for message in (
            Message([MessageSegment.image("file:///secret.png")]),
            Message([MessageSegment.image(b"a"), MessageSegment.image(b"b")]),
            Message([MessageSegment.image(b"a"), MessageSegment("unknown")]),
        ):
            with self.assertRaises((ValueError, UnsupportedCapability)):
                await self.bot.send(self.event.target, message)
        self.api.request.assert_not_awaited()

    async def test_channel_inline_image_uses_multipart_and_dms_has_explicit_route(self):
        target = SessionTarget("qq", APP_ID, "channel", "CHANNEL")
        await self.bot.send(target, Message([MessageSegment.image(b"PNG")]))
        self.assertEqual(self.api.request.await_args.args, ("POST", "/channels/CHANNEL/messages"))
        self.assertEqual(self.api.request.await_args.kwargs["files"]["file_image"][1], b"PNG")
        self.assertIs(self.api.request.await_args.kwargs["upload"], True)
        await self.bot.send(SessionTarget("qq", APP_ID, "private", "dms/GUILD/USER"), Message.text("私信"))
        self.assertEqual(self.api.request.await_args.args[1], "/dms/GUILD/messages")

    async def test_group_does_not_use_channel_message_delete(self):
        await self.bot.send(self.event.target, Message.text("x"))
        await self.bot.delete_message("reply-id")
        self.assertEqual(self.api.request.await_args.args, ("DELETE", "/v2/groups/GROUP_OPENID/messages/reply-id"))

    async def test_mentions_are_explicit_and_channel_text_cannot_inject_mentions(self):
        await self.bot.send(
            self.event.target, Message([MessageSegment.mention("USER_MEMBER"), MessageSegment.text(" hi")])
        )
        self.assertEqual(self.api.request.await_args.kwargs["json"]["content"], "<@USER_MEMBER> hi")
        await self.bot.send(SessionTarget("qq", APP_ID, "channel", "CHANNEL"), Message.text("<@USER> & test"))
        self.assertEqual(self.api.request.await_args.kwargs["json"]["content"], "&lt;@USER&gt; &amp; test")


class QQWebhookQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = Application()
        await self.app.start()
        self.connection = QQWebhookConnection(self.app, APP_ID, QQBotSettings(SECRET), settings())
        self.addAsyncCleanup(self.connection.close)
        self.addAsyncCleanup(self.app.stop)

    async def drain(self):
        tasks = tuple(self.connection.events._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_duplicate_and_close_ownership(self):
        from fastapi import HTTPException

        self.connection.receive(payload())
        self.connection.receive(payload())
        self.assertEqual(len(self.connection.events._tasks), 2)
        await self.drain()
        await self.connection.close()
        self.assertFalse(self.app.bots)
        self.assertTrue(self.connection.events.closed)
        self.assertTrue(self.connection.api._http.is_closed)
        with self.assertRaises(HTTPException):
            self.connection.receive(payload(message_id="third"))

    async def test_channel_queries_bot_identity_before_dispatch(self):
        seen = []

        async def handler(context):
            seen.append(context.event)

        self.app.router.message("capture", handler)
        self.connection.api.request = AsyncMock(return_value={"id": "BOT_USER"})
        self.connection.receive(payload("AT_MESSAGE_CREATE", content="<@BOT_USER> /ping"))
        await self.drain()
        self.assertEqual(seen[0].message.plain_text(), "/ping")
        self.connection.api.request.assert_awaited_once_with("GET", "/users/@me")

    async def test_other_group_with_same_message_id_is_not_deduplicated(self):
        self.connection.receive(payload())
        await self.drain()
        other = payload()
        other["d"]["group_openid"] = "OTHER_GROUP"
        self.connection.receive(other)
        self.assertEqual(len(self.connection.events._tasks), 1)
        await self.drain()

    async def test_all_events_run_without_queue_limit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        active, peak = 0, 0

        async def capture(ctx):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 50:
                entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

        self.app.router.message("capture", capture)
        for index in range(50):
            self.connection.receive(payload(message_id=f"m{index}"))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(peak, 50)
        release.set()
        await self.drain()

    async def test_webhook_later_event_runs_while_first_is_waiting(self):
        entered, later, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def capture(ctx):
            if ctx.event.message_id == "slow":
                entered.set()
                await release.wait()
            else:
                later.set()

        self.app.router.message("capture", capture)
        self.connection.receive(payload(message_id="slow"))
        await asyncio.wait_for(entered.wait(), 1)
        self.connection.receive(payload(message_id="fast"))
        await asyncio.wait_for(later.wait(), 1)
        self.assertFalse(release.is_set())
        release.set()
        await self.drain()

    async def test_close_cancels_inflight_handler_and_releases_http(self):
        entered = asyncio.Event()
        released = asyncio.Event()

        async def handler(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

        self.app.router.message("waiting", handler)
        self.connection.receive(payload())
        await asyncio.wait_for(entered.wait(), 1)
        await self.connection.close()
        self.assertTrue(released.is_set())
        self.assertTrue(self.connection.api._http.is_closed)
        self.assertFalse(self.app.bots)


class QQEntryTests(unittest.TestCase):
    def test_signed_notice_missing_payload_returns_400_without_registration(self):
        result = self.post({"op": 0, "t": "GROUP_MEMBER_ADD", "id": "missing-data"})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.connection.events.started)
        self.assertEqual(self.sent, [])

    def setUp(self):
        from fxbot.config import SYSTEM_DEFAULTS, get_manager
        from fxbot.runtime.server import create_app

        config = deepcopy(SYSTEM_DEFAULTS)
        config["chat"]["enabled"] = False
        self.enterContext(patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(config)))
        self.app = create_app(settings())
        self.client = self.enterContext(TestClient(self.app))
        self.connection = self.app.state.services.qq.connections[APP_ID]
        self.sent = []

        async def handle(request):
            if str(request.url) == AUTH_URL:
                return httpx.Response(200, json={"access_token": "fake-access", "expires_in": "3600"})
            self.sent.append(request)
            if request.url.path == "/users/@me":
                return httpx.Response(200, json={"id": "BOT_USER"})
            return httpx.Response(200, json={"id": "sent-" + str(len(self.sent))})

        self.client.portal.call(self.connection.api._http.aclose)
        self.connection.api._http = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    def post(self, data, **kwargs):
        raw, headers = signed(data, **kwargs)
        return self.client.post("/qq/webhook", content=raw, headers=headers)

    def drain_events(self):
        async def wait():
            tasks = tuple(self.connection.events._tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        self.client.portal.call(wait)

    def test_integer_member_join_timestamp_enters_router_and_passive_reply(self):
        async def joined(context):
            self.assertEqual(context.event.user_id, "MEMBER")
            self.assertEqual(int(context.event.occurred_at.timestamp()), 1784276757)
            await context.send("welcome")

        self.app.state.runtime.router.event("test-member-join", "member_joined", joined)
        frame = {
            "op": 0,
            "t": "GROUP_MEMBER_ADD",
            "id": "member-join-event",
            "d": {
                "group_openid": "GROUP",
                "member_openid": "MEMBER",
                "timestamp": 1784276757,
            },
        }
        self.assertEqual(self.post(frame).status_code, 200)
        self.drain_events()
        body = json.loads(self.sent[-1].content)
        self.assertEqual(body["event_id"], "member-join-event")
        self.assertNotIn("msg_id", body)
        self.assertNotIn("msg_seq", body)

    def test_signed_interaction_enters_core_and_reply_uses_event_id(self):
        async def clicked(context):
            self.assertEqual(context.event.metadata["qq_data"]["data"]["resolved"]["button_id"], "approve")
            await context.send("clicked")

        self.app.state.runtime.router.event("test-click", "interaction", clicked)
        frame = {
            "op": 0,
            "t": "INTERACTION_CREATE",
            "id": "interaction-envelope",
            "d": {
                "id": "interaction-id",
                "application_id": APP_ID,
                "chat_type": 1,
                "group_openid": "GROUP",
                "group_member_openid": "MEMBER",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": {"resolved": {"button_id": "approve"}},
            },
        }
        self.assertEqual(self.post(frame).status_code, 200)
        self.drain_events()
        body = json.loads(self.sent[-1].content)
        self.assertEqual(body["event_id"], "interaction-envelope")
        self.assertNotIn("msg_seq", body)
        self.assertNotIn("msg_id", body)

    def test_signed_group_ping_enters_core_router_and_replies_via_official_api(self):
        response = self.post(payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"op": 12, "d": 0})
        self.drain_events()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].url.path, "/v2/groups/GROUP_OPENID/messages")
        self.assertEqual(
            {key: value for key, value in json.loads(self.sent[0].content).items() if key != "timestamp"},
            {
                "content": "pong",
                "msg_type": 0,
                "msg_id": "msg=1",
                "msg_seq": 1,
            },
        )
        self.assertEqual(self.sent[0].headers["Authorization"], "QQBot fake-access")
        self.assertEqual(self.post(payload()).status_code, 200)
        self.drain_events()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.app.state.runtime.bots, (self.connection.client,))

    def test_c2c_and_channel_and_dms_ping_use_distinct_official_paths(self):
        cases = (
            ("C2C_MESSAGE_CREATE", "/ping", "/v2/users/USER_OPENID/messages"),
            ("AT_MESSAGE_CREATE", "<@BOT_USER> /ping", "/channels/CHANNEL_ID/messages"),
            ("DIRECT_MESSAGE_CREATE", "/ping", "/dms/GUILD_ID/messages"),
        )
        for kind, content, path in cases:
            self.assertEqual(self.post(payload(kind, message_id=kind, content=content)).status_code, 200)
            self.drain_events()
            self.assertEqual(self.sent[-1].url.path, path)
            self.assertEqual(json.loads(self.sent[-1].content)["content"], "pong")

    def test_no_signature_tampered_wrong_bot_and_stale_requests_rejected(self):
        raw, headers = signed(payload())
        self.assertEqual(self.client.post("/qq/webhook", content=raw, headers={"X-Bot-Appid": APP_ID}).status_code, 403)
        self.assertEqual(self.client.post("/qq/webhook", content=raw + b" ", headers=headers).status_code, 403)
        self.assertEqual(self.post(payload(), secret="wrong").status_code, 403)
        self.assertEqual(self.post(payload(), app_id="999").status_code, 403)
        self.assertEqual(self.post(payload(), timestamp="1").status_code, 403)
        self.assertEqual(self.app.state.runtime.bots, ())
        self.assertEqual(self.sent, [])

    def test_challenge_response_has_no_token_request_and_does_not_register_bot(self):
        timestamp = str(int(time.time()))
        response = self.client.post(
            "/qq/webhook",
            headers={"X-Bot-Appid": APP_ID},
            json={"op": 13, "d": {"plain_token": "challenge", "event_ts": timestamp}},
        )
        self.assertEqual(response.status_code, 200)
        self.connection.signer._key.public_key().verify(
            bytes.fromhex(response.json()["signature"]), (timestamp + "challenge").encode()
        )
        self.assertEqual(self.app.state.runtime.bots, ())
        self.assertFalse(self.sent)

    def test_challenge_rejects_arbitrary_dispatch_as_nonce(self):
        response = self.client.post(
            "/qq/webhook",
            headers={"X-Bot-Appid": APP_ID},
            json={
                "op": 13,
                "d": {
                    "plain_token": json.dumps(payload()),
                    "event_ts": str(int(time.time())),
                },
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.app.state.runtime.bots)
        self.assertFalse(self.sent)

    def test_unknown_events_acknowledged_invalid_messages_rejected_and_not_cached(self):
        unknown = payload()
        unknown["t"] = "UNSUPPORTED_EVENT"
        self.assertEqual(self.post(unknown).status_code, 200)
        invalid = payload()
        del invalid["d"]["author"]["member_openid"]
        self.assertEqual(self.post(invalid).status_code, 400)
        self.assertFalse(self.connection.events._tasks)
        self.assertFalse(self.app.state.runtime.bots)

    def test_unsupported_like_is_explicit_without_network_write(self):
        self.assertEqual(self.post(payload(content="#赞我")).status_code, 200)
        self.drain_events()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(json.loads(self.sent[0].content)["content"], "当前平台不支持点赞")

    def test_oversized_body_rejected_before_parsing_or_network(self):
        response = self.client.post("/qq/webhook", headers={"X-Bot-Appid": APP_ID}, content=b"x" * (1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.sent)
        self.assertFalse(self.app.state.runtime.bots)

    def test_malformed_json_and_signed_wrong_opcode_rejected(self):
        self.assertEqual(
            self.client.post("/qq/webhook", headers={"X-Bot-Appid": APP_ID}, content=b"{").status_code, 400
        )
        self.assertEqual(self.post({"op": 2, "d": {}}).status_code, 400)
        self.assertFalse(self.sent)

    def test_same_appid_as_onebot_bot_never_shares_connection(self):
        from fxbot.platforms.onebot11 import OneBot11Client

        bot = OneBot11Client(APP_ID, AsyncMock())
        self.client.portal.call(lambda: self.app.state.runtime.register(bot, close=AsyncMock()))
        self.assertEqual(self.post(payload()).status_code, 200)
        self.drain_events()
        self.assertEqual({item.platform for item in self.app.state.runtime.bots}, {"qq", "onebot11"})
