"""原生聊天边界：身份、媒体、发送与显式路由安装。"""

from __future__ import annotations

import sys
import unittest
import subprocess
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from fxbot.chat import router, message_adapter
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
from fxbot.chat.tools import ToolError, ToolContext, ToolRuntime
from fxbot.chat.types import ChatRequest
from fxbot.chat.session import ChatSessionStore
from fxbot.chat.tools.file import send_file_tool
from fxbot.core.application import Application
from fxbot.chat.session_hooks import resolve_session_id, register_session_resolver
from fxbot.chat.tools.forward import view_forward_message_tool
from fxbot.platforms.onebot11 import OneBot11Client
from fxbot.chat.message_receive import sender
from fxbot.config.system_defaults import SYSTEM_DEFAULTS
from fxbot.chat.focus.group_runtime import FocusSender


def event(**changes) -> Event:
    """构造真实核心事件，不提供旧框架的字段别名。"""
    original = Event(
        None,
        "10",
        "message",
        "onebot11",
        "100",
        "200",
        "用户",
        SessionTarget("onebot11", "100", "group", "300"),
        Message.text("你好"),
        to_me=True,
    )
    return replace(original, **changes)


def client() -> OneBot11Client:
    """客户端只连接测试动作接收器。"""
    return OneBot11Client("100", AsyncMock(return_value={"message_id": 11}))


class NativeMessageTests(unittest.IsolatedAsyncioTestCase):
    """协议查询只经过已声明的客户端能力。"""

    async def test_console_name_lookup_is_scoped_to_application_and_exact_connection(self) -> None:
        from fxbot.console.queries.memory import chat_name

        for expected in ("第一实例", "第二实例"):
            app = Application()
            await app.start()
            try:
                bot = client()
                app.register(bot, close=AsyncMock())
                with patch.object(bot, "target_name", AsyncMock(return_value=expected)) as lookup:
                    self.assertEqual(await chat_name("onebot11:100:scope:group:300", app), expected)
                    self.assertEqual(await chat_name("onebot11:100:scope:group:300", app), expected)
                    self.assertEqual(await chat_name("onebot12.qq:100:group:300", app), "")
                    lookup.assert_awaited_once_with(event().target)
            finally:
                await app.stop()

    async def test_identity_is_connection_scoped_and_requires_platform_id(self) -> None:
        bot, incoming = client(), event()
        identity = message_adapter.event_identity(bot, incoming)
        self.assertEqual(identity.session_id, "onebot11:100:group:300")
        self.assertEqual(identity.dedupe_key, "msg:10")
        with self.assertRaises(ValueError):
            message_adapter.event_identity(bot, event(message_id=None))
        with self.assertRaises(ValueError):
            message_adapter.event_identity(OneBot11Client("101", AsyncMock()), incoming)

    async def test_mentions_use_original_message_and_declared_lookup(self) -> None:
        bot = client()
        original = Message(
            [
                MessageSegment.mention("100"),
                MessageSegment.mention("201"),
                MessageSegment.mention("201"),
                MessageSegment("mention_all"),
            ]
        )
        incoming = event(original_message=original, to_me=False)
        self.assertEqual(message_adapter.mention_flags(bot, incoming), (True, True))
        with patch.object(bot, "member_name", AsyncMock(return_value="成员")) as lookup:
            mentions = await message_adapter.collect_mentions(bot, incoming, "300")
        self.assertEqual([(item.user_id, item.name) for item in mentions], [("201", "成员")])
        lookup.assert_awaited_once_with("300", "201")

    async def test_current_images_resolve_once_and_memory_excludes_emoji(self) -> None:
        bot = client()
        incoming = event(
            message=Message(
                [
                    MessageSegment.text("看图"),
                    MessageSegment.image(b"photo"),
                    MessageSegment("image", {"file": b"emoji", "is_emoji": True}),
                ]
            )
        )
        with (
            patch.object(bot, "resolve_media", AsyncMock(side_effect=lambda segment: segment.data["file"])) as resolve,
            patch.object(message_adapter, "transcribe_event", AsyncMock(return_value="")),
            patch.object(message_adapter, "load_images", AsyncMock(return_value=["encoded"])),
            patch.object(message_adapter, "max_image_count", return_value=4),
        ):
            request = await message_adapter.adapt_message_event(bot, incoming, trigger_kind="mention")
        self.assertEqual(resolve.await_count, 2)
        self.assertEqual(request.metadata["raw_image_paths"], ["1"])
        self.assertEqual(request.metadata["raw_images"], [b"photo"])
        self.assertEqual(request.images, ["encoded"])
        self.assertEqual(request.occurred_at.tzinfo, None)

    async def test_quote_images_do_not_become_current_image_memory(self) -> None:
        bot = client()
        incoming = event(message_id="quote-case", reply=ReplyTarget("20"))
        quote = QuotedMessage("20", "201", "作者", Message([MessageSegment.image(b"quoted")]))
        with (
            patch.object(bot, "get_reply", AsyncMock(return_value=quote)),
            patch.object(message_adapter, "transcribe_event", AsyncMock(return_value="")),
            patch.object(message_adapter, "load_images", AsyncMock(return_value=["encoded"])) as images,
            patch.object(message_adapter, "max_image_count", return_value=4),
        ):
            request = await message_adapter.adapt_message_event(bot, incoming, trigger_kind="mention")
        images.assert_awaited_once_with([b"quoted"])
        self.assertEqual(request.metadata["raw_images"], [])
        self.assertEqual(request.metadata["quote_snapshot"]["message_id"], "20")

    async def test_internal_media_error_is_not_silently_skipped(self) -> None:
        bot = client()
        with patch.object(bot, "resolve_media", AsyncMock(side_effect=RuntimeError("bug"))):
            with self.assertRaisesRegex(RuntimeError, "bug"):
                await message_adapter.image_sources_for_event(bot, event(message=Message([MessageSegment.image(b"x")])))

    async def test_session_resolver_caches_on_slotted_event_and_preserves_connection(self) -> None:
        bot, incoming = client(), event()
        calls = []

        def scoped(bot, incoming, base):
            calls.append(base)
            return "onebot11:100:scope:group:300"

        unregister = register_session_resolver("native-test", scoped)
        try:
            for _ in range(2):
                self.assertEqual(message_adapter.session_id_for_event(bot, incoming), "onebot11:100:scope:group:300")
            self.assertEqual(len(calls), 1)
        finally:
            unregister()

    async def test_session_resolver_failure_cannot_retry_in_another_namespace(self) -> None:
        incoming = event()
        unregister = register_session_resolver("native-test", lambda *_: "onebot11:101:scope:group:300")
        try:
            with self.assertRaises(ValueError):
                resolve_session_id(client(), incoming, incoming.target.key())
        finally:
            unregister()
        with self.assertRaises(RuntimeError):
            resolve_session_id(client(), incoming, incoming.target.key())


class NativeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """发送器不是 Matcher 外壳，引用与文件能力不通过失败探测。"""

    async def test_sender_preserves_exact_target_and_has_no_finish_stub(self) -> None:
        bot, incoming = client(), event()
        output = FocusSender(bot, incoming.target)
        self.assertFalse(hasattr(output, "finish"))
        with patch.object(bot, "send", AsyncMock(return_value={"message_id": "11"})) as send:
            await output.send("[CQ:at,qq=200]")
        target, message = send.await_args.args
        self.assertIs(target, incoming.target)
        self.assertEqual(message.segments, [MessageSegment.text("[CQ:at,qq=200]")])

    async def test_payload_builds_core_segments_without_adapter(self) -> None:
        request = ChatRequest(
            "onebot11:100:group:300", "200", "300", "你好", "turn", "msg:10", "mention", message_id="10"
        )
        with patch.object(sender, "_should_quote", return_value=True):
            payload = sender.first_payload(None, request, ToolRuntime(bot=client()), "回答", ["201"])
        self.assertEqual([segment.type for segment in payload.segments], ["reply", "mention", "text"])

    async def test_file_requires_capability_before_creating_cache_file(self) -> None:
        bot = client()
        bot.capabilities = Capabilities()
        with patch("fxbot.chat.tools.file.cache_dir") as directory:
            with self.assertRaises(ToolError):
                await send_file_tool(ToolContext(), ToolRuntime(bot=bot, event=event()), "内容.txt", "内容")
        directory.assert_not_called()

    async def test_uploaded_file_is_unique_and_removed_after_send(self) -> None:
        bot = client()
        bot.capabilities = bot.capabilities.provide(Capability.FILE, bot)
        paths = []

        async def upload(target, path, name):
            paths.append(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "内容")
            self.assertEqual(target, event().target)

        with patch.object(bot, "send_file", AsyncMock(side_effect=upload)):
            for _ in range(2):
                result = await send_file_tool(ToolContext(), ToolRuntime(bot=bot, event=event()), "内容.txt", "内容")
                self.assertTrue(result["sent"])
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_forward_does_not_guess_id_on_message_lookup_failure(self) -> None:
        bot = client()
        incoming = event(reply=ReplyTarget("20"))
        with (
            patch.object(bot, "get_forward_ids", AsyncMock(side_effect=ValueError("bad response"))),
            patch.object(bot, "get_forward_messages", AsyncMock()) as read,
        ):
            with self.assertRaises(ValueError):
                await view_forward_message_tool(ToolContext(), ToolRuntime(bot=bot, event=incoming), msg_id="20")
        read.assert_not_called()

    async def test_forward_rejects_unrelated_message(self) -> None:
        with patch("fxbot.chat.tools.forward.chat_repository.message_by_identity", AsyncMock(return_value=None)):
            with self.assertRaises(ToolError):
                await view_forward_message_tool(
                    ToolContext(session_id=event().target.key()), ToolRuntime(bot=client(), event=event()), msg_id="999"
                )

    async def test_native_dispatch_passes_application_and_authorizer_to_tool_runtime(self) -> None:
        application = Application()
        authorizer = SimpleNamespace(check=AsyncMock(return_value=True))
        router.install(application, authorizer)
        incoming, bot = event(), client()
        service = SimpleNamespace(
            session_store=ChatSessionStore(), ensure_hydrated=AsyncMock(), process_and_send=AsyncMock()
        )
        request = ChatRequest(incoming.target.key(), "200", "300", "你好", "turn", "msg:10", "mention")
        config = deepcopy(SYSTEM_DEFAULTS["chat"])
        with (
            patch.object(router, "_chat_cfg", return_value=config),
            patch.object(router, "can_reply", return_value=True),
            patch.object(router, "direct_reply_mode", return_value=True),
            patch.object(router, "chat_service", service),
            patch.object(router, "_focus_or_detach", AsyncMock(return_value=False)),
            patch.object(router, "adapt_message_event", AsyncMock(return_value=request)),
        ):
            result = await application.router.dispatch(bot, incoming)
        self.assertEqual(result.failed, ())
        self.assertEqual(result.handled, ("chat:reply",))
        runtime = service.process_and_send.await_args.args[1]
        self.assertIs(runtime.application, application)
        self.assertIs(runtime.authorizer, authorizer)


class ChatIndependenceTests(unittest.TestCase):
    """新解释器拒绝旧框架及未选择的模型 SDK。"""

    def test_chat_imports_do_not_load_framework_or_model_sdk(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = """
import importlib.abc, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
class RejectLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        legacy = fullname.startswith(('fxbot.adapter', 'fxbot.plugin.'))
        sdk = fullname.split('.')[0] in {'openai', 'anthropic', 'google'}
        if legacy or sdk:
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectLegacy())
with tempfile.TemporaryDirectory() as directory, patch('fxbot.utils.paths.project_root', return_value=Path(directory)):
    import fxbot.chat.router, fxbot.chat.commands, fxbot.chat.recall
    from fxbot.core.application import Application
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    app = Application()
    auth = SimpleNamespace(check=AsyncMock(return_value=True), guard=lambda _: AsyncMock(return_value=True))
    fxbot.chat.router.install(app, auth)
    fxbot.chat.commands.install(app, auth)
    fxbot.chat.recall.install(app)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c", script, str(root / "src")],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
