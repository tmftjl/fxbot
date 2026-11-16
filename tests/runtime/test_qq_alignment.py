"""QQ 普通群/C2C 参考接口的协议契约、事件与分片闭环。"""

from __future__ import annotations

import json
import asyncio
import unittest
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx

from fxbot.core import Message, Capability, SessionTarget, MessageSegment
from fxbot.core.context import bind_event, reset_event
from fxbot.platforms.qq import QQ_API
from tests.runtime.test_qq import APP_ID, SECRET, payload
from fxbot.core.application import Application
from fxbot.platforms.qq.api import QQHTTP, QQAPIError
from fxbot.runtime.settings import Settings
from fxbot.platforms.qq.codec import NOTICE_TYPES, parse_event
from fxbot.platforms.qq.client import QQClient
from fxbot.platforms.qq.config import QQBotSettings
from fxbot.platforms.qq.events import QQEventStream
from fxbot.platforms.qq.gateway import QQGateway, GatewayProtocolError
from fxbot.platforms.qq.message import ark, embed, media, stream, keyboard, markdown
from tests.runtime.test_qq_gateway import Socket, ready, gateway_info

NOW = "2026-11-16T10:00:00+08:00"


def notice(kind, event_id="event-1", **changes):
    data = {
        "openid": "USER",
        "group_openid": "GROUP",
        "op_member_openid": "OPERATOR",
        "member_openid": "MEMBER",
        "timestamp": 1784276757,
        "apply_at": NOW,
        "audit_time": NOW,
        "audit_id": "AUDIT",
        "join_request_id": "REQUEST",
    }
    if kind == "INTERACTION_CREATE":
        data.update(
            timestamp=NOW,
            id="button-event",
            application_id=APP_ID,
            chat_type=1,
            group_member_openid="MEMBER",
            data={"resolved": {"button_id": "button", "button_data": "yes"}},
        )
    data.update(changes)
    return {"op": 0, "t": kind, "id": event_id, "d": data}


class QQNoticeTests(unittest.IsolatedAsyncioTestCase):
    def test_all_nonchannel_notice_types_are_preserved(self):
        for kind, event_type in NOTICE_TYPES.items():
            with self.subTest(kind=kind):
                source = notice(kind)
                event = parse_event(source, bot_id=APP_ID)
                self.assertEqual(event.event_type, event_type)
                self.assertFalse(event.is_message)
                self.assertEqual(event.metadata["qq_data"], source["d"])
                self.assertIsNot(event.metadata["qq_data"], source["d"])
                self.assertEqual(event.event_id, "event-1")

    def test_group_and_c2c_interaction_identity_is_not_appid(self):
        group = parse_event(notice("INTERACTION_CREATE"), bot_id=APP_ID)
        private = parse_event(notice("INTERACTION_CREATE", chat_type=2, user_openid="PRIVATE"), bot_id=APP_ID)
        self.assertEqual(group.user_id, "MEMBER")
        self.assertEqual(private.target.target_id, "PRIVATE")
        with self.assertRaises(ValueError):
            parse_event(notice("INTERACTION_CREATE", application_id="other"), bot_id=APP_ID)

    async def test_notice_dedup_uses_event_id_not_absent_message_id(self):
        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        bot = QQClient(APP_ID, SimpleNamespace(request=AsyncMock()))
        events = QQEventStream(app, bot, bot_user_id="", capacity=5, concurrency=2)
        events.start(events.close)
        self.addAsyncCleanup(events.close)
        seen = []

        async def handle(ctx):
            seen.append(ctx.event.event_id)

        app.router.event("joined", "member_joined", handle)
        events.receive(notice("GROUP_MEMBER_ADD", "one"))
        events.receive(notice("GROUP_MEMBER_ADD", "two"))
        events.receive(notice("GROUP_MEMBER_ADD", "one"))
        await events.queue.join()
        self.assertEqual(seen, ["one", "two"])

    async def test_replayed_inflight_event_commits_only_after_original_finishes(self):
        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        bot = QQClient(APP_ID, SimpleNamespace(request=AsyncMock()))
        events = QQEventStream(app, bot, bot_user_id="", capacity=3, concurrency=1)
        events.start(events.close)
        self.addAsyncCleanup(events.close)
        entered, release = asyncio.Event(), asyncio.Event()
        completed = []

        async def handle(ctx):
            entered.set()
            await release.wait()

        app.router.event("slow", "member_joined", handle)
        frame = notice("GROUP_MEMBER_ADD")
        events.receive(frame, processed=lambda: completed.append("original"))
        await entered.wait()
        events.receive(frame, processed=lambda: completed.append("replay"))
        self.assertEqual(completed, [])
        release.set()
        await events.queue.join()
        self.assertEqual(completed, ["original", "replay"])


class QQAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = SimpleNamespace(request=AsyncMock(return_value={"ok": True}), put_upload_part=AsyncMock())
        self.bot = QQClient(APP_ID, self.http)
        self.actions = self.bot.capabilities.require(QQ_API)

    async def test_management_paths_queries_and_bodies(self):
        cases = [
            ("me", {}, "GET", "/users/@me"),
            ("url_get", {}, "GET", "/gateway"),
            ("shard_url_get", {}, "GET", "/gateway/bot"),
            ("get_group_info", {"group_id": "GROUP"}, "GET", "/v2/groups/GROUP/info"),
            ("get_group_bot_state", {"group_id": "GROUP"}, "GET", "/v2/groups/GROUP/bot_state"),
            ("get_group_mute_setting", {"group_id": "GROUP"}, "GET", "/v2/groups/GROUP/restrict_chat_setting"),
            (
                "set_group_members_mute",
                {"group_id": "GROUP", "members": []},
                "POST",
                "/v2/groups/GROUP/restrict_chat_setting",
            ),
            (
                "get_group_join_request_list",
                {"group_id": "GROUP", "cursor": "opaque=/cursor", "limit": 10},
                "GET",
                "/v2/groups/GROUP/join_request_list",
            ),
            (
                "approval_join_request",
                {"group_id": "GROUP", "member_openid": "MEMBER", "op": "approve", "join_request_id": "req"},
                "POST",
                "/v2/groups/GROUP/approval_join_request/MEMBER",
            ),
            ("get_group_members", {"group_openid": "GROUP", "cursor": "cursor"}, "GET", "/v2/groups/GROUP/members"),
            ("post_group_members", {"group_id": "GROUP", "start_index": 1}, "POST", "/v2/groups/GROUP/members"),
            (
                "get_group_member",
                {"group_openid": "GROUP", "member_openid": "MEMBER"},
                "GET",
                "/v2/groups/GROUP/members/MEMBER",
            ),
            (
                "batch_remove_group_members",
                {"group_openid": "GROUP", "member_openids": ["MEMBER"]},
                "POST",
                "/v2/groups/GROUP/batch_remove_members",
            ),
            ("get_group_member_blacklist", {"group_openid": "GROUP"}, "GET", "/v2/groups/GROUP/member_blacklist"),
            (
                "post_group_member_blacklist",
                {"group_openid": "GROUP", "op": "del", "member_openids": ["MEMBER"]},
                "POST",
                "/v2/groups/GROUP/member_blacklist",
            ),
            ("put_interaction", {"interaction_id": "button=/id", "code": 0}, "PUT", "/interactions/button%3D%2Fid"),
            (
                "delete_group_message",
                {"group_openid": "GROUP", "message_id": "msg=/id"},
                "DELETE",
                "/v2/groups/GROUP/messages/msg%3D%2Fid",
            ),
            (
                "delete_c2c_message",
                {"openid": "USER", "message_id": "msg=/id"},
                "DELETE",
                "/v2/users/USER/messages/msg%3D%2Fid",
            ),
        ]
        for method, kwargs, verb, path in cases:
            with self.subTest(method=method):
                await getattr(self.actions, method)(**kwargs)
                self.assertEqual(self.http.request.await_args.args, (verb, path))
                values = self.http.request.await_args.kwargs
                if "cursor" in kwargs:
                    self.assertEqual(values["params"]["cursor"], kwargs["cursor"])
                if "op" in kwargs:
                    self.assertEqual(values["json"]["op"], kwargs["op"])

    async def test_raw_message_api_preserves_all_reference_fields(self):
        common = dict(
            msg_type=2,
            content="hi",
            markdown={"content": "# hi"},
            keyboard={"id": "template"},
            ark={"template_id": 1},
            embed={"prompt": "hi"},
            media={"file_info": "media"},
            message_reference={"message_id": "REFIDX_1"},
            event_id="event",
            msg_id="msg",
            msg_seq=2,
            timestamp=1794790000,
        )
        for name, target in (
            ("post_group_messages", {"group_openid": "GROUP"}),
            ("post_c2c_messages", {"openid": "USER"}),
        ):
            await getattr(self.actions, name)(**target, **common)
            self.assertEqual(self.http.request.await_args.kwargs["json"], common)
        await self.actions.post_c2c_messages(openid="USER", msg_type=0, stream={"index": 1, "state": 1})
        self.assertEqual(self.http.request.await_args.kwargs["json"]["stream"], {"index": 1, "state": 1})

    async def test_group_everyone_mention_matches_reference(self):
        self.http.request.return_value = {"id": "sent"}
        await self.bot.send(SessionTarget("qq", APP_ID, "group", "GROUP"), Message([MessageSegment("mention_all")]))
        self.assertEqual(self.http.request.await_args.kwargs["json"]["content"], "@everyone")

    async def test_high_level_advanced_messages_and_recall(self):
        self.http.request.return_value = {"id": "sent"}
        for kind, segment, expected in (
            ("group", markdown("# title"), 2),
            ("group", keyboard({"id": "k"}), 2),
            ("group", ark({"template_id": 1}), 3),
            ("private", embed({"prompt": "hi"}), 4),
            ("private", stream(state=1, index=1), 0),
            ("group", media("existing"), 7),
        ):
            target = SessionTarget("qq", APP_ID, kind, "TARGET")
            await self.bot.send(target, Message([segment]))
            self.assertEqual(self.http.request.await_args.kwargs["json"]["msg_type"], expected)
            await self.bot.delete_message("sent")
            route = "groups" if kind == "group" else "users"
            self.assertEqual(self.http.request.await_args.args, ("DELETE", f"/v2/{route}/TARGET/messages/sent"))

    async def test_notice_replies_use_event_id_not_msg_id(self):
        self.http.request.return_value = {"id": "sent"}
        for kind in (
            "INTERACTION_CREATE",
            "GROUP_ADD_ROBOT",
            "GROUP_MEMBER_ADD",
            "GROUP_MSG_RECEIVE",
            "FRIEND_ADD",
            "C2C_MSG_RECEIVE",
        ):
            with self.subTest(kind=kind):
                event = parse_event(notice(kind), bot_id=APP_ID)
                token = bind_event(self.bot, event)
                try:
                    await self.bot.send(event.target, Message.text("hello"))
                finally:
                    reset_event(token)
                body = self.http.request.await_args.kwargs["json"]
                self.assertEqual(body["event_id"], event.event_id)
                self.assertNotIn("msg_id", body)
                self.assertNotIn("msg_seq", body)
                self.assertFalse(self.bot._sequence)

    async def test_unsupported_passive_notice_does_not_borrow_event_id(self):
        self.http.request.return_value = {"id": "sent"}
        event = parse_event(notice("GROUP_JOIN_REQUEST"), bot_id=APP_ID)
        token = bind_event(self.bot, event)
        try:
            await self.bot.send(event.target, Message.text("proactive"))
        finally:
            reset_event(token)
        body = self.http.request.await_args.kwargs["json"]
        self.assertNotIn("event_id", body)
        self.assertNotIn("msg_id", body)
        self.assertNotIn("msg_seq", body)

    async def test_partial_moderation_does_not_claim_unsupported_admin_operations(self):
        self.http.request.return_value = {"members": []}
        self.assertNotIn(Capability.MODERATION, self.bot.capabilities)
        self.assertIn(Capability.MEMBER_MODERATION, self.bot.capabilities)
        await self.bot.capabilities.require(Capability.MEMBER_MODERATION).ban("GROUP", "USER", 60)
        body = self.http.request.await_args.kwargs["json"]
        self.assertEqual(body["members"][0]["op"], "add")
        await self.bot.ban("GROUP", "USER", 0)
        self.assertEqual(self.http.request.await_args.kwargs["json"]["members"][0]["op"], "del")
        await self.bot.kick("GROUP", "USER", reject_add_request=True)
        self.assertTrue(self.http.request.await_args.kwargs["json"]["add_to_member_blacklist"])

    async def test_concurrent_repeat_mute_reads_state_then_uses_update_without_retry(self):
        members, operations = [], []

        async def request(method, path, **kwargs):
            await asyncio.sleep(0)
            if method == "GET":
                return {"members": list(members)}
            member = kwargs["json"]["members"][0]
            operations.append(member["op"])
            members[:] = [] if member["op"] == "del" else [member]
            return {}

        self.http.request.side_effect = request
        await asyncio.gather(self.bot.ban("GROUP", "USER", 60), self.bot.ban("GROUP", "USER", 120))
        await self.bot.ban("GROUP", "USER", 0)
        self.assertEqual(operations, ["add", "update", "del"])
        self.assertEqual(
            [call.args[0] for call in self.http.request.await_args_list], ["GET", "POST", "GET", "POST", "POST"]
        )

    async def test_mute_state_read_failure_or_bad_response_does_not_write(self):
        for result in (QQAPIError(503), {}, {"members": "invalid"}):
            self.http.request.reset_mock()
            self.http.request.side_effect = result if isinstance(result, Exception) else None
            self.http.request.return_value = result
            with self.subTest(result=result), self.assertRaises((QQAPIError, ValueError)):
                await self.bot.ban("GROUP", "USER", 60)
            self.assertEqual(self.http.request.await_count, 1)
            self.assertEqual(self.http.request.await_args.args[0], "GET")

    async def test_invalid_target_is_rejected_before_send_or_upload(self):
        for value in (".", ".."):
            target = SessionTarget("qq", APP_ID, "group", value)
            with self.assertRaises(ValueError):
                await self.bot.send(target, Message.text("bad"))
            with self.assertRaises(ValueError):
                await self.actions.post_group_upload(value, 4, "file.txt", b"x")
            with self.assertRaises(ValueError):
                await self.actions.get_group_info(group_id=value)
        self.http.request.assert_not_awaited()

    async def test_reply_selects_matching_reference_not_first_element(self):
        source = payload("GROUP_MESSAGE_CREATE", content="<@quoted-user> answer")
        source["d"].update(
            message_type=103,
            message_scene={"ext": ["msg_idx=REFIDX_CURRENT", "ref_msg_idx=REFIDX_QUOTED"]},
            mentions=[{"scope": "single", "id": "quoted-user", "is_you": False, "member_openid": "QUOTED"}],
            msg_elements=[
                {"msg_idx": "other", "content": "wrong"},
                {
                    "msg_idx": "REFIDX_QUOTED",
                    "content": "quoted text",
                    "author": {"id": "quoted-user", "member_openid": "QUOTED", "username": "quoted author"},
                },
            ],
        )
        event = parse_event(source, bot_id=APP_ID)
        self.assertEqual(event.message.plain_text(), "answer")
        self.assertNotEqual(event.user_name, "quoted author")
        quoted = await self.bot.get_reply(event)
        self.assertEqual(quoted.message.plain_text(), "quoted text")
        self.assertEqual(quoted.sender_id, "QUOTED")
        self.http.request.return_value = {"id": "sent", "ext_info": {"ref_idx": "REFIDX_SENT"}}
        token = bind_event(self.bot, event)
        try:
            result = await self.bot.send(
                event.target, Message([MessageSegment.reply(event.message_id), MessageSegment.text("hi")])
            )
        finally:
            reset_event(token)
        self.assertEqual(
            self.http.request.await_args.kwargs["json"]["message_reference"], {"message_id": "REFIDX_CURRENT"}
        )
        self.assertEqual(result["ext_info"], {"ref_idx": "REFIDX_SENT"})

    async def test_large_bytes_use_multipart_upload_not_inline_base64(self):
        self.bot.actions.post_group_upload = AsyncMock(return_value={"file_info": "large"})
        self.http.request.return_value = {"id": "sent"}
        content = b"x" * (10 * 1024 * 1024 + 1)
        await self.bot.send(SessionTarget("qq", APP_ID, "group", "GROUP"), Message([MessageSegment.image(content)]))
        self.bot.actions.post_group_upload.assert_awaited_once()
        self.assertEqual(self.http.request.await_count, 1)
        self.assertEqual(self.http.request.await_args.kwargs["json"]["media"], {"file_info": "large"})

    async def test_group_members_roles_and_muted_data_are_normalized(self):
        self.http.request.side_effect = [
            {
                "members": [{"member_openid": "ONE", "username": "one", "member_role": "member"}],
                "next_cursor": "second",
            },
            {"members": [{"member_openid": "TWO", "username": "two", "member_role": "admin"}]},
            {"member_role": "admin"},
            {"members": [{"member_openid": "ONE", "username": "one", "mute_expire_at": "2099-01-01T00:00:00Z"}]},
        ]
        members = await self.bot.get_members("GROUP")
        self.assertEqual([member["user_id"] for member in members], ["ONE", "TWO"])
        self.assertEqual(await self.bot.member_role("GROUP", APP_ID), "admin")
        muted = await self.bot.get_muted_members("GROUP")
        self.assertEqual(muted[0]["user_id"], "ONE")
        self.assertGreater(muted[0]["remaining"], 0)


class QQUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_explicit_part_storage_error_is_retried_and_bounded(self):
        for code, attempts in ((40093001, 3), (123, 1)):
            prepared = {
                "upload_id": "up",
                "block_size": 1,
                "parts": [{"index": 0, "presigned_url": "https://upload.test/0"}],
                "upload_config": {"concurrency": 1, "retry_timeout": 1, "retry_delay": 0},
            }
            http = SimpleNamespace(
                request=AsyncMock(side_effect=[prepared] + [QQAPIError(400, code)] * attempts),
                put_upload_part=AsyncMock(),
            )
            bot = QQClient(APP_ID, http)
            with self.assertRaises(QQAPIError):
                await bot.actions.post_group_upload("GROUP", 4, "x", b"a")
            self.assertEqual(http.request.await_count, attempts + 1)
            http.put_upload_part.assert_awaited_once()

    async def test_group_and_private_upload_parts_then_merge_without_credentials_on_put(self):
        for group in (True, False):
            requests = []

            def server(request):
                requests.append(request)
                if request.url.host == "upload.example.test":
                    self.assertNotIn("authorization", request.headers)
                    self.assertNotIn("x-union-appid", request.headers)
                    return httpx.Response(200, content=b"uploaded")
                if request.url.path.endswith("upload_prepare"):
                    return httpx.Response(
                        200,
                        json={
                            "upload_id": "up",
                            "block_size": 2,
                            "parts": [
                                {"index": index, "presigned_url": f"https://upload.example.test/{index}"}
                                for index in range(3)
                            ],
                            "upload_config": {"concurrency": 2, "retry_delay": 0, "retry_timeout": 1},
                        },
                    )
                if request.url.path.endswith("/files"):
                    return httpx.Response(200, json={"file_info": "finished"})
                return httpx.Response(200, content=b"")

            api = QQHTTP(APP_ID, SECRET, httpx.AsyncClient(transport=httpx.MockTransport(server)))
            api.token = AsyncMock(return_value="secret-token")
            bot = QQClient(APP_ID, api)
            try:
                if group:
                    result = await bot.actions.post_group_upload("GROUP", 4, "test.txt", b"abcdef", False)
                else:
                    result = await bot.actions.post_c2c_upload("USER", 4, "test.txt", b"abcdef", False)
                self.assertEqual(result, {"file_info": "finished"})
                self.assertEqual(
                    [request.content for request in requests if request.method == "PUT"], [b"ab", b"cd", b"ef"]
                )
                self.assertEqual(json.loads(requests[-1].content), {"upload_id": "up", "srv_send_msg": False})
            finally:
                await api.close()

    async def test_failure_cancels_and_joins_other_parts_without_merge(self):
        active = asyncio.Event()
        cleaned = asyncio.Event()
        api = SimpleNamespace(request=AsyncMock(), put_upload_part=AsyncMock())
        bot = QQClient(APP_ID, api)
        api.request.return_value = {
            "upload_id": "up",
            "block_size": 1,
            "parts": [{"index": i, "presigned_url": f"https://upload.test/{i}"} for i in range(2)],
            "upload_config": {"concurrency": 2, "retry_timeout": 1, "retry_delay": 0},
        }

        async def put(url, content, **kwargs):
            if content == b"a":
                await active.wait()
                raise QQAPIError(500)
            active.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        api.put_upload_part.side_effect = put
        with self.assertRaises(QQAPIError):
            await bot.actions.post_group_upload("GROUP", 4, "x", b"ab")
        self.assertTrue(cleaned.is_set())
        self.assertEqual(api.request.await_count, 1)


class QQShardTests(unittest.IsolatedAsyncioTestCase):
    async def test_permanent_protocol_failure_stops_all_shards_and_reclaims_owner(self):
        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        peer_entered, peer_cleaned = asyncio.Event(), asyncio.Event()
        config = QQBotSettings(SECRET, mode="websocket")
        owner = QQGateway(app, APP_ID, config, Settings())
        self.addAsyncCleanup(owner.close)
        owner.api.request = AsyncMock(return_value=gateway_info(shards=2))

        async def run_child(child):
            if child.config.shard[0] == 0:
                await peer_entered.wait()
                raise GatewayProtocolError("permanent shard configuration failure")
            peer_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                peer_cleaned.set()

        original = QQGateway.run

        async def route_run(child):
            return await original(child) if child is owner else await run_child(child)

        from unittest.mock import patch

        with patch.object(QQGateway, "run", route_run), self.assertRaises(GatewayProtocolError):
            await owner.run()
        self.assertTrue(peer_cleaned.is_set())
        self.assertTrue(owner.api._http.is_closed)
        self.assertFalse(app.bots)

    async def test_auto_shards_share_one_client_and_close_together(self):
        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        sockets = []
        identified = asyncio.Event()

        @asynccontextmanager
        async def connector(url, **kwargs):
            socket = Socket()
            sockets.append(socket)
            original = socket.send

            async def send(raw):
                await original(raw)
                packet = json.loads(raw)
                if packet["op"] == 2:
                    frame = ready()
                    frame["d"]["session_id"] = f"session-{packet['d']['shard'][0]}"
                    socket.feed(frame)
                    if len(sockets) == 2:
                        identified.set()

            socket.send = send
            socket.feed({"op": 10, "d": {"heartbeat_interval": 10000}})
            yield socket

        config = QQBotSettings(SECRET, mode="websocket")
        owner = QQGateway(app, APP_ID, config, Settings(), connector=connector)
        self.addAsyncCleanup(owner.close)
        owner.api.token = AsyncMock(return_value="fake")
        owner.api.request = AsyncMock(return_value=gateway_info(shards=2))
        owner._pause = AsyncMock()
        owner.start()
        await asyncio.wait_for(identified.wait(), 2)
        for _ in range(100):
            if len(owner._online_shards) == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(owner._online_shards), 2)
        self.assertEqual(len(app.bots), 1)
        self.assertIs(owner._children[0]._stream, owner._children[1]._stream)
        self.assertIs(owner._children[0].api, owner.api)
        self.assertEqual(owner._budget_remaining, 8)
        old = app.bots[0]
        await owner._children[0]._disconnect()
        self.assertIs(app.bots[0], old)
        old._online()
        await owner._children[1]._disconnect()
        self.assertFalse(app.bots)
        with self.assertRaises(ConnectionError):
            old._online()
        await owner.close()
        self.assertFalse(app.bots)
        self.assertTrue(owner.api._http.is_closed)
