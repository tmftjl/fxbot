"""gscore 原生回归：入站转换、下行发送、回执、meta、连接与适配器扩展。"""

from __future__ import annotations

import json
import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from fxbot.core import (
    Event,
    Message,
    Capability,
    ReplyTarget,
    Capabilities,
    QuotedMessage,
    SessionTarget,
    MessageSegment,
)
from fxbot.plugins.gscore.client import GscoreService, ConnectionSettings, build_ws_url
from fxbot.plugins.gscore.config import validate_config
from fxbot.plugins.gscore.models import (
    Message as WireMessage,
    MessageSend,
    MessageReceive,
    decode_send,
    encode_receive,
    recall_receipt,
)
from fxbot.plugins.gscore.inbound import build_receive, build_meta_receive
from fxbot.plugins.gscore.adapters import register, find_send, find_event, unregister
from fxbot.plugins.gscore.dispatch import is_log_frame, dispatch_outgoing
from fxbot.plugins.gscore.adapters.base import SendResult, CoreAdapter

PLATFORM = "onebot11"
BOT_ID = "100"
USER_ID = "200"
GROUP_ID = "300"


class ApplicationStub:
    """受管任务接口的最小替身。"""

    def __init__(self, bots: tuple[Any, ...] = ()) -> None:
        self.bots = bots
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coroutine: Any, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def cancel(self, task: asyncio.Task) -> bool:
        if task.done():
            return False
        return task.cancel()


class ServicesStub:
    """只实现适配器实际读取的窄接口。"""

    def __init__(self, bots: tuple[Any, ...] = (), superusers: tuple[str, ...] = ()) -> None:
        self.application = ApplicationStub(bots)
        self.configuration = SimpleNamespace(
            get_system=lambda: {"system": {"superusers": list(superusers), "bot_admins": []}}
        )


class BotStub:
    def __init__(self, platform: str, bot_id: str, result: Any = None) -> None:
        self.platform = platform
        self.bot_id = bot_id
        self.capabilities = Capabilities()
        self.message_transform = lambda target, message: message
        self.send = AsyncMock(return_value=result)


def make_event(
    *,
    platform: str = PLATFORM,
    bot_id: str = BOT_ID,
    kind: str = "group",
    target_id: str = GROUP_ID,
    user_id: str = USER_ID,
    message: Message | None = None,
    event_type: str = "message",
    message_id: str = "m1",
    metadata: dict[str, Any] | None = None,
    reply: ReplyTarget | None = None,
    original_message: Message | None = None,
) -> Event:
    target = SessionTarget(platform, bot_id, kind, target_id)
    content = Message([]) if message is None else message
    return Event(
        event_id=None,
        message_id=message_id,
        event_type=event_type,
        platform=platform,
        bot_id=bot_id,
        user_id=user_id,
        user_name="tester",
        target=target,
        message=content,
        reply=reply,
        metadata={} if metadata is None else metadata,
        original_message=original_message if original_message is not None else content,
        occurred_at=datetime.now(timezone.utc),
    )


class InboundTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_segments_identity_and_superuser_pm(self) -> None:
        message = Message(
            [
                MessageSegment.text("#你好"),
                MessageSegment.mention("999"),
                MessageSegment.image("https://img.example.test/a.jpg"),
                MessageSegment.reply("m0"),
            ]
        )
        superuser = SessionTarget(PLATFORM, BOT_ID, "private", USER_ID).key()
        event = make_event(
            message=message,
            reply=ReplyTarget("m0"),
            metadata={"sender_role": "owner"},
        )
        receive = await build_receive(
            event,
            bot=BotStub(PLATFORM, BOT_ID),
            services=ServicesStub(superusers=(superuser,)),
            reply_image=True,
        )
        self.assertIsNotNone(receive)
        assert receive is not None
        self.assertEqual(receive.bot_id, "onebot")
        self.assertEqual(receive.bot_self_id, BOT_ID)
        self.assertEqual(receive.msg_id, "m1")
        self.assertEqual(receive.user_type, "group")
        self.assertEqual(receive.group_id, GROUP_ID)
        self.assertEqual(receive.user_id, USER_ID)
        self.assertEqual(receive.user_pm, 1)
        self.assertEqual([item.type for item in receive.content], ["text", "at", "image", "reply"])
        self.assertEqual(receive.content[0].data, "你好")
        self.assertEqual(receive.content[1].data, "999")
        self.assertEqual(receive.content[2].data, "link://https://img.example.test/a.jpg")
        self.assertEqual(receive.content[3].data, "m0")

    async def test_quoted_images_follow_reply_image_config(self) -> None:
        bot = BotStub(PLATFORM, BOT_ID)
        quoted = QuotedMessage(
            "m0",
            "999",
            "quoted",
            Message([MessageSegment.image("https://img.example.test/quoted.jpg")]),
        )
        bot.get_reply = AsyncMock(return_value=quoted)
        bot.capabilities = Capabilities().provide(Capability.REPLY, bot)
        event = make_event(
            message=Message([MessageSegment.reply("m0"), MessageSegment.text("hi")]),
            reply=ReplyTarget("m0"),
        )
        receive = await build_receive(event, bot=bot, services=ServicesStub(), reply_image=True)
        assert receive is not None
        self.assertEqual([item.type for item in receive.content], ["image", "reply", "text"])
        self.assertEqual(receive.content[0].data, "link://https://img.example.test/quoted.jpg")
        receive = await build_receive(event, bot=bot, services=ServicesStub(), reply_image=False)
        assert receive is not None
        self.assertEqual([item.type for item in receive.content], ["reply", "text"])

    async def test_group_role_maps_user_pm(self) -> None:
        for role, expected in (("owner", 2), ("admin", 3), ("member", 6), (None, 6)):
            metadata = {} if role is None else {"sender_role": role}
            event = make_event(message=Message.text("hi"), metadata=metadata)
            receive = await build_receive(
                event,
                bot=BotStub(PLATFORM, BOT_ID),
                services=ServicesStub(),
                reply_image=True,
            )
            assert receive is not None
            self.assertEqual(receive.user_pm, expected, role)


class DownlinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_send_target_and_segments(self) -> None:
        bot = BotStub("onebot11", BOT_ID, {"message_id": 123})
        services = ServicesStub(bots=(bot,))
        msg = MessageSend(
            bot_id="onebot",
            bot_self_id=BOT_ID,
            target_type="group",
            target_id=GROUP_ID,
            content=[
                WireMessage("text", "hi"),
                WireMessage("at", "200"),
                WireMessage("reply", "m9"),
                WireMessage("image", "base64://aGk="),
            ],
        )
        adapter = find_send("onebot")
        self.assertIsNotNone(adapter)
        assert adapter is not None
        result = await adapter.send(services, msg)
        self.assertEqual(result.recall_id, "123")
        target, message = bot.send.await_args.args
        self.assertEqual(target, SessionTarget("onebot11", BOT_ID, "group", GROUP_ID))
        self.assertEqual([segment.type for segment in message.segments], ["text", "mention", "reply", "image"])
        self.assertEqual(message.segments[0].data, {"text": "hi"})
        self.assertEqual(message.segments[1].data, {"user_id": "200", "name": ""})
        self.assertEqual(message.segments[3].data, {"file": b"hi"})

    async def test_qqgroup_send_target_and_advanced_segments(self) -> None:
        bot = BotStub("qq", "appid", {"message_id": "qm1"})
        services = ServicesStub(bots=(bot,))
        msg = MessageSend(
            bot_id="qqgroup",
            bot_self_id="appid",
            target_type="group",
            target_id="group-openid",
            msg_id="passive-id",
            content=[
                WireMessage("text", "你好"),
                WireMessage("markdown", "# hi"),
                WireMessage("template_markdown", {"template_id": "t1", "para": {"title": "标题"}}),
                WireMessage("template_buttons", "kb1"),
                WireMessage("at", "member-1"),
            ],
        )
        adapter = find_send("qqgroup")
        self.assertIsNotNone(adapter)
        assert adapter is not None
        result = await adapter.send(services, msg)
        self.assertEqual(result.recall_id, "qm1")
        target, message = bot.send.await_args.args
        self.assertEqual(target, SessionTarget("qq", "appid", "group", "group-openid"))
        self.assertEqual(
            [segment.type for segment in message.segments],
            ["qq:passive", "text", "qq:markdown", "qq:markdown", "qq:keyboard", "mention"],
        )
        self.assertEqual(message.segments[0].data, {"msg_id": "passive-id"})
        self.assertEqual(message.segments[2].data, {"content": "# hi"})
        self.assertEqual(message.segments[3].data["custom_template_id"], "t1")
        self.assertEqual(message.segments[3].data["params"], [{"key": "title", "values": ["标题"]}])
        self.assertEqual(message.segments[4].data, {"id": "kb1"})
        self.assertEqual(message.segments[5].data, {"user_id": "member-1", "name": ""})


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    def test_receipt_keeps_echo_and_optional_id_list(self) -> None:
        msg = MessageSend(bot_id="onebot", bot_self_id=BOT_ID, echo="e1")
        receipt = recall_receipt(msg, ["a", "b"])
        self.assertEqual(receipt.bot_id, "onebot")
        self.assertEqual(receipt.bot_self_id, BOT_ID)
        self.assertEqual(receipt.user_id, "")
        self.assertEqual(receipt.content[0].type, "recall_message_id")
        self.assertEqual(receipt.content[0].data, {"echo": "e1", "id": ["a", "b"]})
        encoded = encode_receive(receipt)
        self.assertIsInstance(encoded, bytes)
        payload = json.loads(encoded)
        self.assertEqual(payload["content"][0]["data"], {"echo": "e1", "id": ["a", "b"]})
        none_receipt = recall_receipt(msg, None)
        self.assertIsNone(none_receipt.content[0].data["id"])

    def test_decode_send_accepts_log_and_control_frames(self) -> None:
        decoded = decode_send(
            json.dumps(
                {
                    "bot_id": "onebot",
                    "target_type": "group",
                    "target_id": 300,
                    "content": [{"type": "text", "data": "hi"}],
                    "echo": 7,
                }
            )
        )
        self.assertEqual(decoded.target_id, "300")
        self.assertEqual(decoded.echo, "7")
        assert decoded.content is not None
        self.assertEqual(decoded.content[0].type, "text")
        log_frame = MessageSend(bot_id="core", content=[WireMessage("log_info", "hello")])
        self.assertTrue(is_log_frame(log_frame))

    async def test_log_frame_and_unknown_platform_are_ignored(self) -> None:
        bot = BotStub("onebot11", BOT_ID, {"message_id": "unused"})
        services = ServicesStub(bots=(bot,))
        result = await dispatch_outgoing(services, MessageSend(bot_id="core", content=[WireMessage("log_info", "x")]))
        self.assertIsNone(result)
        bot.send.assert_not_awaited()
        unknown = MessageSend(
            bot_id="telegram",
            bot_self_id="tb",
            target_type="direct",
            target_id="u",
            content=[WireMessage("text", "x")],
        )
        self.assertIsNone(await dispatch_outgoing(services, unknown))


class MetaTests(unittest.IsolatedAsyncioTestCase):
    def test_member_and_poke_meta_conversion(self) -> None:
        services = ServicesStub()
        joined = make_event(
            event_type="member_joined",
            metadata={"notice_type": "group_increase", "operator_id": "100", "sub_type": "approve"},
        )
        receive = build_meta_receive(joined, services=services)
        self.assertIsNotNone(receive)
        assert receive is not None
        self.assertEqual(receive.bot_id, "onebot")
        self.assertEqual(receive.user_type, "group")
        self.assertEqual(receive.group_id, GROUP_ID)
        self.assertEqual(receive.user_id, USER_ID)
        self.assertEqual(receive.content[0].type, "meta-user_join_group")
        self.assertEqual(
            receive.content[0].data,
            {"user_id": USER_ID, "group_id": GROUP_ID, "operator_id": "100", "sub_type": "approve"},
        )

        left = make_event(event_type="member_left", metadata={"notice_type": "group_decrease", "sub_type": "leave"})
        receive = build_meta_receive(left, services=services)
        assert receive is not None
        self.assertEqual(receive.content[0].type, "meta-user_exit_group")
        self.assertEqual(receive.content[0].data["sub_type"], "leave")

        poke = make_event(
            event_type="notification",
            metadata={"notice_type": "notify", "sub_type": "poke", "target_id": "100"},
        )
        receive = build_meta_receive(poke, services=services)
        assert receive is not None
        self.assertEqual(receive.content[0].type, "meta-poke")
        self.assertEqual(
            receive.content[0].data,
            {"user_id": USER_ID, "target_id": "100", "group_id": GROUP_ID},
        )

    def test_qq_group_meta_uses_qqgroup(self) -> None:
        services = ServicesStub()
        event = make_event(
            platform="qq",
            bot_id="appid",
            kind="group",
            target_id="group-openid",
            user_id="member-openid",
            event_type="member_joined",
            metadata={"operator_id": "op", "sub_type": "approve"},
        )
        receive = build_meta_receive(event, services=services)
        assert receive is not None
        self.assertEqual(receive.bot_id, "qqgroup")
        self.assertEqual(receive.user_type, "group")
        self.assertEqual(receive.group_id, "group-openid")


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def test_url_and_config_validation(self) -> None:
        self.assertEqual(build_ws_url("localhost", 8765, "FxBot"), "ws://localhost:8765/ws/FxBot")
        self.assertEqual(
            build_ws_url("ws://localhost/", 8765, "FxBot", "a b"), "ws://localhost:8765/ws/FxBot?token=a%20b"
        )
        settings = ConnectionSettings.from_config(
            {"enabled": True, "host": "localhost", "port": 8765, "token": "", "bot_id": "FxBot", "reply_image": False}
        )
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.url, "ws://localhost:8765/ws/FxBot")
        self.assertNotIn("token", settings.display_url)
        validate_config({"enabled": True, "host": "x", "port": 8765, "token": "", "bot_id": "B", "reply_image": True})
        with self.assertRaises(ValueError):
            validate_config({"port": 0})
        with self.assertRaises(ValueError):
            validate_config({"enabled": "yes"})

    async def test_reconnect_backoff_without_network(self) -> None:
        delays: list[float] = []
        attempts: list[str] = []

        class Connector:
            def __call__(self, url: str, **kwargs: Any) -> Any:
                attempts.append(url)
                raise OSError("gsuid_core down")

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) >= 2:
                raise asyncio.CancelledError

        service = GscoreService(ServicesStub(), connector=Connector(), sleep=fake_sleep)
        await service.maintain(
            {"enabled": True, "host": "localhost", "port": 8765, "token": "", "bot_id": "FxBot", "reply_image": True}
        )
        task = service.task
        self.assertIsNotNone(task)
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        assert service.settings is not None
        self.assertEqual(attempts, [service.settings.url, service.settings.url])
        self.assertEqual(delays, [1.0, 2.0])
        self.assertFalse(service.connected)
        await service.close()

    async def test_disabled_config_does_not_start_connection(self) -> None:
        service = GscoreService(ServicesStub())
        await service.maintain(
            {"enabled": False, "host": "localhost", "port": 8765, "token": "", "bot_id": "FxBot", "reply_image": True}
        )
        self.assertIsNone(service.task)
        self.assertFalse(service.connected)
        await service.close()

    async def test_upstream_frames_are_binary(self) -> None:
        """gsuid_core 只读 receive_bytes，上行必须是 bytes（二进制帧）。"""
        frames: list[Any] = []

        class FakeWebSocket:
            async def send(self, payload: Any) -> None:
                frames.append(payload)

        service = GscoreService(ServicesStub())
        service._queue.put_nowait(
            MessageReceive(
                bot_id="onebot",
                bot_self_id=BOT_ID,
                user_id=USER_ID,
                content=[WireMessage("text", "hi")],
            )
        )
        task = asyncio.create_task(service._send_loop(FakeWebSocket()))
        try:
            for _ in range(100):
                if frames:
                    break
                await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(frames), 1)
        self.assertIsInstance(frames[0], bytes)
        self.assertEqual(json.loads(frames[0])["content"], [{"type": "text", "data": "hi"}])


class FakeAdapter(CoreAdapter):
    key = "fake-test"
    name = "Fake"

    def platform_matches(self, platform: str) -> bool:
        return platform == "fake-platform"

    def matches_send(self, bot_id: str) -> bool:
        return bot_id == "fakebot"

    def receive_bot_id(self, event: Event) -> str:
        return "fakebot"

    async def send(self, services: Any, msg: MessageSend) -> SendResult:
        return SendResult("fake-id")


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_adapter_plugs_into_dispatch_without_connector_changes(self) -> None:
        adapter = FakeAdapter()
        register(adapter)
        try:
            self.assertIs(find_send("fakebot"), adapter)
            services = ServicesStub(bots=(BotStub("fake-platform", "fb"),))
            result = await dispatch_outgoing(
                services,
                MessageSend(
                    bot_id="fakebot",
                    bot_self_id="fb",
                    target_type="direct",
                    target_id="u",
                    content=[WireMessage("text", "x")],
                ),
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.recall_id, "fake-id")
        finally:
            unregister(adapter)
        self.assertIsNone(find_send("fakebot"))
        self.assertIsNone(
            find_event(make_event(platform="unknown-platform", bot_id="ub", kind="private", target_id="u"))
        )


if __name__ == "__main__":
    unittest.main()
