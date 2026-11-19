"""gscore 原生回归：入站转换、下行发送、回执、meta、连接与适配器扩展。"""

from __future__ import annotations

import json
import asyncio
import unittest
from io import BytesIO
from types import SimpleNamespace
from base64 import b64encode
from typing import Any
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from PIL import Image

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
from fxbot.platforms.onebot11 import OneBot11Client
from fxbot.platforms.qq.client import QQClient
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


def png_source(color: str = "red") -> str:
    """生成用于长图渲染的 16x16 PNG data URI。"""
    buffer = BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
    return "base64://" + b64encode(buffer.getvalue()).decode("ascii")


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
        self.assertEqual(receive.content[2].data, "https://img.example.test/a.jpg")
        self.assertEqual(receive.content[3].data, "m0")

    async def test_all_inbound_media_urls_stay_bare_for_downstream_plugins(self) -> None:
        message = Message(
            [
                MessageSegment.image("https://media.example.test/a.jpg"),
                MessageSegment.voice("https://media.example.test/a.mp3"),
                MessageSegment.audio("https://media.example.test/b.mp3"),
                MessageSegment("video", {"file": "https://media.example.test/c.mp4"}),
                MessageSegment("file", {"file": "https://media.example.test/d.zip", "name": "d.zip"}),
            ]
        )
        receive = await build_receive(
            make_event(message=message),
            bot=BotStub(PLATFORM, BOT_ID),
            services=ServicesStub(),
            reply_image=False,
        )
        assert receive is not None
        self.assertEqual([item.type for item in receive.content], ["image", "record", "record", "video", "file"])
        self.assertEqual(
            [item.data for item in receive.content],
            [
                "https://media.example.test/a.jpg",
                "https://media.example.test/a.mp3",
                "https://media.example.test/b.mp3",
                "https://media.example.test/c.mp4",
                "d.zip|https://media.example.test/d.zip",
            ],
        )

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
        self.assertEqual(receive.content[0].data, "https://img.example.test/quoted.jpg")
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
            ["qq:passive", "text", "qq:markdown", "qq:markdown", "qq:keyboard"],
        )
        self.assertEqual(message.segments[0].data, {"msg_id": "passive-id"})
        self.assertEqual(message.segments[2].data, {"content": "# hi"})
        self.assertEqual(message.segments[3].data["custom_template_id"], "t1")
        self.assertEqual(message.segments[3].data["params"], [{"key": "title", "values": ["标题"]}])
        self.assertEqual(message.segments[4].data, {"id": "kb1"})

    async def test_qqgroup_long_text_node_is_merged_into_one_passive_message(self) -> None:
        bot = BotStub("qq", "appid", {"message_id": "node-result"})
        msg = MessageSend(
            bot_id="qqgroup",
            bot_self_id="appid",
            target_type="group",
            target_id="group-openid",
            msg_id="passive-id",
            content=[WireMessage("node", [WireMessage("text", str(index)) for index in range(10)])],
        )
        await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        self.assertEqual(bot.send.await_count, 1)
        message = bot.send.await_args.args[1]
        self.assertEqual(message.plain_text(), "\n".join(str(index) for index in range(10)))
        self.assertTrue(any(segment.type == "qq:passive" for segment in message.segments))

    async def test_onebot_node_uses_native_forward_message(self) -> None:
        sender = AsyncMock(return_value={"message_id": "forward-result"})
        bot = OneBot11Client("100", sender)
        msg = MessageSend(
            bot_id="onebot",
            bot_self_id="100",
            target_type="group",
            target_id=GROUP_ID,
            content=[
                WireMessage(
                    "node",
                    [
                        {"type": "text", "data": "第一段"},
                        {"type": "text", "data": "第二段"},
                    ],
                )
            ],
        )
        result = await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        self.assertEqual(result.recall_id, "forward-result")
        sender.assert_awaited_once()
        action, params = sender.await_args.args
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual(len(params["messages"]), 2)
        self.assertEqual(params["messages"][0]["data"]["content"][0]["data"]["text"], "第一段")
        self.assertEqual(params["messages"][1]["data"]["content"][0]["data"]["text"], "第二段")

    async def test_onebot_node_is_sent_before_normal_text(self) -> None:
        sender = AsyncMock(return_value={"message_id": "message-result"})
        bot = OneBot11Client("100", sender)
        msg = MessageSend(
            bot_id="onebot",
            bot_self_id="100",
            target_type="group",
            target_id=GROUP_ID,
            content=[
                WireMessage("text", "前面"),
                WireMessage("node", [{"type": "text", "data": "转发内容"}]),
                WireMessage("text", "后面"),
            ],
        )
        await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        self.assertEqual(sender.await_count, 2)
        self.assertEqual(sender.await_args_list[0].args[0], "send_group_forward_msg")
        self.assertEqual(sender.await_args_list[1].args[0], "send_group_msg")
        texts = [
            segment["data"]["text"]
            for segment in sender.await_args_list[1].args[1]["message"]
            if segment["type"] == "text"
        ]
        self.assertEqual(texts, ["前面", "后面"])

    async def test_qqgroup_alternating_text_images_pair_into_passive_messages(self) -> None:
        bot = BotStub("qq", "appid", {"message_id": "node-result"})
        content = []
        for index in range(4):
            content.append(WireMessage("text", f"第{index}段"))
            content.append(WireMessage("image", png_source()))
        msg = MessageSend(
            bot_id="qqgroup",
            bot_self_id="appid",
            target_type="group",
            target_id="group-openid",
            msg_id="passive-id",
            content=[WireMessage("node", content)],
        )
        await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        self.assertEqual(bot.send.await_count, 4)
        for index, call in enumerate(bot.send.await_args_list):
            message = call.args[1]
            self.assertEqual(message.plain_text(), f"第{index}段")
            self.assertTrue(any(segment.type == "image" for segment in message.segments))
            self.assertTrue(any(segment.type == "qq:passive" for segment in message.segments))

    async def test_qqgroup_many_images_are_rendered_as_long_images(self) -> None:
        bot = BotStub("qq", "appid", {"message_id": "node-result"})
        msg = MessageSend(
            bot_id="qqgroup",
            bot_self_id="appid",
            target_type="group",
            target_id="group-openid",
            msg_id="passive-id",
            content=[WireMessage("node", [WireMessage("image", png_source()) for _ in range(7)])],
        )
        await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        self.assertEqual(bot.send.await_count, 1)
        message = bot.send.await_args.args[1]
        self.assertTrue(any(segment.type == "qq:passive" for segment in message.segments))
        images = [segment for segment in message.segments if segment.type == "image"]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].data["file"].startswith(b"\x89PNG"))

    async def test_qqgroup_dozens_of_images_fit_in_at_most_five_pages(self) -> None:
        bot = BotStub("qq", "appid", {"message_id": "node-result"})
        msg = MessageSend(
            bot_id="qqgroup",
            bot_self_id="appid",
            target_type="group",
            target_id="group-openid",
            msg_id="passive-id",
            content=[WireMessage("node", [WireMessage("image", png_source()) for _ in range(30)])],
        )
        await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        self.assertGreaterEqual(bot.send.await_count, 1)
        self.assertLessEqual(bot.send.await_count, 5)
        for call in bot.send.await_args_list:
            self.assertTrue(any(segment.type == "image" for segment in call.args[1].segments))
            self.assertTrue(any(segment.type == "qq:passive" for segment in call.args[1].segments))

    async def test_qqgroup_node_forward_is_sent_as_separate_messages(self) -> None:
        bot = BotStub("qq", "appid", {"message_id": "node-result"})
        msg = MessageSend(
            bot_id="qqgroup",
            bot_self_id="appid",
            target_type="group",
            target_id="group-openid",
            content=[
                WireMessage(
                    "node",
                    [
                        {"type": "text", "data": "第一段"},
                        {"type": "image", "data": "https://media.example.test/node.jpg"},
                    ],
                )
            ],
        )
        await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        self.assertEqual(bot.send.await_count, 2)
        self.assertEqual(bot.send.await_args_list[0].args[1].plain_text(), "第一段")
        self.assertEqual(bot.send.await_args_list[1].args[1].segments[0].type, "image")
        self.assertEqual(
            bot.send.await_args_list[1].args[1].segments[0].data["file"],
            "https://media.example.test/node.jpg",
        )

    async def test_qq_login_reply_omits_mentions_in_group_and_c2c(self) -> None:
        text = "[鸣潮]【神上园凛】特征码【105044421】登录成功!"
        for target_type, route in (("group", "groups"), ("direct", "users")):
            with self.subTest(target_type=target_type):
                api = SimpleNamespace(request=AsyncMock(return_value={"id": "login-result"}))
                bot = QQClient("appid", api)
                msg = MessageSend(
                    bot_id="qqgroup",
                    bot_self_id="appid",
                    target_type=target_type,
                    target_id="target-openid",
                    msg_id="login-message",
                    content=[
                        WireMessage("at", "7A8EE78D6817E942ED69510F61E6515B"),
                        WireMessage("text", text),
                    ],
                )
                result = await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
                self.assertEqual(result.recall_id, "login-result")
                api.request.assert_awaited_once()
                request = api.request.await_args
                self.assertEqual(request.args[:2], ("POST", f"/v2/{route}/target-openid/messages"))
                self.assertEqual(request.kwargs["json"]["content"], text)
                self.assertEqual(request.kwargs["json"]["msg_id"], "login-message")

    async def test_qq_channel_keeps_mentions(self) -> None:
        bot = BotStub("qq", "appid", {"message_id": "channel-result"})
        msg = MessageSend(
            bot_id="qqguild",
            bot_self_id="appid",
            target_type="group",
            target_id="channel-id",
            content=[WireMessage("at", {"user_id": "member-id"}), WireMessage("text", "登录成功")],
        )
        await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        target, message = bot.send.await_args.args
        self.assertEqual(target.kind, "channel")
        self.assertEqual(message.mentions(), ["member-id"])
        self.assertEqual(message.plain_text(), "登录成功")

    async def test_qq_direct_channel_message_omits_mentions(self) -> None:
        bot = BotStub("qq", "appid", {"message_id": "dms-result"})
        msg = MessageSend(
            bot_id="qqguild",
            bot_self_id="appid",
            target_type="direct",
            target_id="user-id",
            content=[
                WireMessage("group", "guild-id"),
                WireMessage("at", "member-id"),
                WireMessage("text", "登录成功"),
            ],
        )
        await dispatch_outgoing(ServicesStub(bots=(bot,)), msg)
        target, message = bot.send.await_args.args
        self.assertEqual(target.target_id, "dms/guild-id/user-id")
        self.assertFalse(message.mentions())
        self.assertEqual(message.plain_text(), "登录成功")


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
