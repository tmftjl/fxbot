"""核心数据契约、身份校验和上下文隔离测试。"""

from __future__ import annotations

import sys
import asyncio
import unittest
import subprocess
from types import SimpleNamespace
from pathlib import Path

from fxbot.core import (
    Event,
    Message,
    SessionTarget,
    MessageSegment,
    bind_event,
    reset_event,
    current_event,
    current_event_context,
)


class CoreTypeTests(unittest.TestCase):
    """核心对象只表达业务需要的语义。"""

    def test_message_keeps_segments_and_extracts_text_mentions(self) -> None:
        message = Message(
            [
                MessageSegment.text("你好 "),
                MessageSegment.mention("42", "小明"),
                MessageSegment.text("世界"),
            ]
        )
        self.assertEqual(message.plain_text(), "你好 世界")
        self.assertEqual(message.mentions(), ["42"])

    def test_session_key_separates_platform_bot_kind_and_target(self) -> None:
        target = SessionTarget("test", "bot-1", "group", "100")
        self.assertEqual(target.key(), "test:bot-1:group:100")
        self.assertNotEqual(target, SessionTarget("other", "bot-1", "group", "100"))
        self.assertNotEqual(
            SessionTarget("test:other", "bot", "group", "100").bot_key(),
            SessionTarget("test", "other:bot", "group", "100").bot_key(),
        )
        self.assertNotEqual(
            SessionTarget("test:other", "bot", "private", "100").key(),
            SessionTarget("test", "other:bot", "private", "100").key(),
        )
        self.assertNotEqual(
            SessionTarget("test", "bot", "private", "a:b").key(),
            SessionTarget("test", "bot", "private", "a%3Ab").key(),
        )

    def test_invalid_identity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SessionTarget("", "bot", "private", "user")
        with self.assertRaises(ValueError):
            SessionTarget("test", "bot", "unknown", "user")
        with self.assertRaises(ValueError):
            Event(None, None, "message", "other", "bot", "user", "", SessionTarget("test", "bot", "private", "user"))

    def test_import_in_interpreter_without_site_packages(self) -> None:
        root = Path(__file__).resolve().parents[2] / "src"
        script = (
            "import sys, importlib.util; "
            "sys.path.insert(0, sys.argv[1]); "
            "assert importlib.util.find_spec('nonebot') is None; "
            "from fxbot import core; "
            "assert core.Message.text('hello').plain_text() == 'hello'; "
            "assert not any(n == 'nonebot' or n.startswith('nonebot.') for n in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, str(root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_event_exposes_only_core_fields(self) -> None:
        event = Event(
            event_id="event-1",
            message_id="message-1",
            event_type="message",
            platform="test",
            bot_id="bot-1",
            user_id="7",
            user_name="用户",
            target=SessionTarget("test", "bot-1", "private", "7"),
            message=Message.text("hello"),
        )
        self.assertTrue(event.is_message)
        self.assertFalse(event.is_group)
        self.assertEqual(event.message.plain_text(), "hello")


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_cannot_keep_event_context_alive_after_parent_scope_exits(self) -> None:
        release = asyncio.Event()
        target = SessionTarget("test", "bot", "private", "user")
        event = Event(None, None, "message", "test", "bot", "user", "", target)
        token = bind_event(SimpleNamespace(platform="test", bot_id="bot"), event)

        async def child():
            await release.wait()
            return current_event()

        task = asyncio.create_task(child())
        reset_event(token)
        release.set()
        with self.assertRaises(RuntimeError):
            await task

    """事件上下文必须可嵌套、可恢复且不依赖全局当前 Bot。"""

    async def test_context_is_task_local_and_restored(self) -> None:
        event = Event(
            None,
            None,
            "message",
            "test",
            "bot",
            "user",
            "name",
            SessionTarget("test", "bot", "private", "user"),
        )
        bot = SimpleNamespace(platform="test", bot_id="bot")
        token = bind_event(bot, event)
        try:
            self.assertIs(current_event(), event)
            self.assertIs(current_event_context().bot, bot)

            async def child() -> str:
                return current_event().user_id

            self.assertEqual(await asyncio.create_task(child()), "user")
        finally:
            reset_event(token)
        with self.assertRaises(RuntimeError):
            current_event()

    async def test_nested_context_restores_outer_context(self) -> None:
        target = SessionTarget("test", "bot", "private", "outer")
        outer = Event(None, None, "message", "test", "bot", "outer", "", target)
        inner_target = SessionTarget("test", "bot", "private", "inner")
        inner = Event(None, None, "message", "test", "bot", "inner", "", inner_target)
        bot = SimpleNamespace(platform="test", bot_id="bot")
        outer_token = bind_event(bot, outer)
        try:
            inner_token = bind_event(bot, inner)
            try:
                self.assertEqual(current_event().user_id, "inner")
            finally:
                reset_event(inner_token)
            self.assertEqual(current_event().user_id, "outer")
        finally:
            reset_event(outer_token)

    async def test_parallel_tasks_do_not_overwrite_each_other(self) -> None:
        ready = asyncio.Event()
        arrived = 0

        async def handle(user: str) -> str:
            nonlocal arrived
            target = SessionTarget("test", "bot", "private", user)
            event = Event(None, None, "message", "test", "bot", user, "", target)
            token = bind_event(SimpleNamespace(platform="test", bot_id="bot"), event)
            try:
                arrived += 1
                if arrived == 2:
                    ready.set()
                await ready.wait()
                return current_event().user_id
            finally:
                reset_event(token)

        self.assertEqual(await asyncio.gather(handle("first"), handle("second")), ["first", "second"])
        with self.assertRaises(RuntimeError):
            current_event()

    async def test_binding_another_bot_fails_without_changing_context(self) -> None:
        target = SessionTarget("test", "bot", "private", "user")
        event = Event(None, None, "message", "test", "bot", "user", "", target)
        with self.assertRaises(ValueError):
            bind_event(SimpleNamespace(platform="test", bot_id="another"), event)
        with self.assertRaises(RuntimeError):
            current_event()


if __name__ == "__main__":
    unittest.main()
