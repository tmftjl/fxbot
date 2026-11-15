"""命令路由、准入顺序、权限检查与上下文清理测试。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fxbot.core import Event, Message, SessionTarget, current_event
from fxbot.core.router import Router
from fxbot.core.matching import MatchResult


def event(text: str = "/ping", *, event_type: str = "message") -> Event:
    """创建可直接分发的核心事件。"""
    return Event(
        None,
        "1",
        event_type,
        "test",
        "bot",
        "user",
        "",
        SessionTarget("test", "bot", "private", "user"),
        Message.text(text),
    )


class RouterTests(unittest.IsolatedAsyncioTestCase):
    """每条分发路径必须保留清晰的阻断和异常语义。"""

    def setUp(self) -> None:
        self.bot = SimpleNamespace(platform="test", bot_id="bot", send=AsyncMock())
        self.router = Router()

    async def test_command_word_arguments_and_non_message_filter(self) -> None:
        handler = AsyncMock()
        self.router.command("ping", "/ping", handler)
        for text in ("/pingx", "PING", "/Ping", "", "hello /ping"):
            self.assertEqual((await self.router.dispatch(self.bot, event(text))).handled, ())
        self.assertEqual((await self.router.dispatch(self.bot, event(event_type="notice"))).handled, ())
        await self.router.dispatch(self.bot, event(" /ping  参数 一 "))
        self.assertEqual(handler.await_args.args[0].arguments, "参数 一")

    async def test_priority_and_registration_order_are_stable(self) -> None:
        order = []
        for name, priority in (("late", 99), ("first", 5), ("second", 5)):

            async def handler(context, label=name):
                order.append(label)

            self.router.message(name, handler, priority=priority, block=False)
        result = await self.router.dispatch(self.bot, event())
        self.assertEqual(order, ["first", "second", "late"])
        self.assertEqual(result.handled, tuple(order))

    async def test_block_stops_lower_priority_but_not_same_priority_routes(self) -> None:
        handler, other, fallback = AsyncMock(), AsyncMock(), AsyncMock()
        self.router.message("first", handler, priority=1)
        self.router.message("second", other, priority=1)
        self.router.message("fallback", fallback, priority=99)
        result = await self.router.dispatch(self.bot, event())
        self.assertEqual(result.handled, ("first", "second"))
        other.assert_awaited_once()
        fallback.assert_not_awaited()

    async def test_permission_denial_skips_handler_not_fallback(self) -> None:
        handler, fallback = AsyncMock(), AsyncMock()
        self.router.command("restricted", "/ping", handler, permission=AsyncMock(return_value=False))
        self.router.message("fallback", fallback, priority=99)
        result = await self.router.dispatch(self.bot, event())
        self.assertEqual(result.denied, ("restricted",))
        self.assertEqual(result.handled, ("fallback",))
        handler.assert_not_awaited()
        fallback.assert_awaited_once()

    async def test_guards_require_explicit_true(self) -> None:
        handler = AsyncMock()
        self.router.message("restricted", handler, permission=AsyncMock(return_value=1))
        result = await self.router.dispatch(self.bot, event())
        self.assertEqual(result.denied, ("restricted",))
        handler.assert_not_awaited()

    async def test_permission_is_checked_before_rule(self) -> None:
        permission = AsyncMock(return_value=True)
        rule = AsyncMock(return_value=False)
        self.router.message("miss", AsyncMock(), rule=rule, permission=permission)
        self.assertEqual((await self.router.dispatch(self.bot, event())).handled, ())
        permission.assert_awaited_once()
        rule.assert_awaited_once()
        self.assertIs(permission.await_args.args[0], rule.await_args.args[0])

    async def test_preprocessors_run_in_order_and_reject_before_matching(self) -> None:
        order = []

        async def first(value):
            self.assertIs(current_event(), value)
            order.append("first")
            return True

        async def second(value):
            order.append("second")
            return False

        self.router.add_preprocessor(first)
        self.router.add_preprocessor(second)
        handler = AsyncMock()
        self.router.message("blocked", handler)
        result = await self.router.dispatch(self.bot, event())
        self.assertEqual(order, ["first", "second"])
        self.assertTrue(result.rejected)
        handler.assert_not_awaited()
        with self.assertRaises(RuntimeError):
            current_event()

    async def test_finish_ends_handler_without_forcing_block(self) -> None:
        async def handler(context):
            await context.finish("hello")
            self.fail("finish 后不能继续执行")

        fallback = AsyncMock()
        self.router.command("ping", "/ping", handler, block=False)
        self.router.message("fallback", fallback, priority=99)
        result = await self.router.dispatch(self.bot, event())
        self.assertEqual(result.handled, ("ping", "fallback"))
        self.assertEqual(self.bot.send.await_args.args[1].plain_text(), "hello")

    async def test_failure_is_logged_without_customer_message(self) -> None:
        for stage in ("handler", "permission", "rule", "match", "preprocessor"):
            router = Router()
            failure = AsyncMock(side_effect=ValueError("secret internal path"))
            options = {}
            handler = failure if stage == "handler" else AsyncMock()
            if stage in {"permission", "rule"}:
                options[stage] = failure

            def match(value):
                if stage == "match":
                    raise ValueError("secret internal path")
                return MatchResult()

            router.add("broken", handler, match=match, **options)
            if stage == "preprocessor":
                router.add_preprocessor(failure)
            with self.subTest(stage=stage), self.assertLogs("fxbot.core.router", level="ERROR") as logs:
                result = await router.dispatch(self.bot, event())
            self.assertTrue(result.blocked)
            self.assertTrue(result.failed)
            self.assertIn("secret internal path", "\n".join(logs.output))
            self.bot.send.assert_not_awaited()
            with self.assertRaises(RuntimeError):
                current_event()

    async def test_cancel_propagates_and_cleans_context(self) -> None:
        async def handler(context):
            raise asyncio.CancelledError

        self.router.message("cancel", handler)
        with self.assertRaises(asyncio.CancelledError):
            await self.router.dispatch(self.bot, event())
        with self.assertRaises(RuntimeError):
            current_event()

    async def test_send_failure_stops_fallback_and_is_logged(self) -> None:
        self.bot.send.side_effect = OSError("offline")

        async def handler(context):
            await context.finish("hello")

        fallback = AsyncMock()
        self.router.message("send", handler, block=False)
        self.router.message("fallback", fallback, priority=99)
        with self.assertLogs("fxbot.core.router", level="ERROR"):
            result = await self.router.dispatch(self.bot, event())
        self.assertEqual(result.failed, ("send",))
        fallback.assert_not_awaited()

    async def test_invalid_registration_and_wrong_bot(self) -> None:
        handler = AsyncMock()
        self.router.command("ping", "/ping", handler)
        with self.assertRaises(ValueError):
            self.router.command("ping", "/other", handler)
        with self.assertRaises(ValueError):
            self.router.command("bad", "two words", handler)
        with self.assertRaises(ValueError):
            self.router.message("bad", handler, priority=True)
        with self.assertRaises(ValueError):
            await self.router.dispatch(SimpleNamespace(platform="test", bot_id="other"), event())
        handler.assert_not_awaited()

    async def test_registration_during_dispatch_only_affects_next_event(self) -> None:
        added = AsyncMock()

        async def register(context):
            if not getattr(register, "done", False):
                self.router.message("new", added, priority=99)
                register.done = True

        self.router.message("register", register, block=False)
        first = await self.router.dispatch(self.bot, event())
        self.assertEqual(first.handled, ("register",))
        added.assert_not_awaited()
        second = await self.router.dispatch(self.bot, event())
        self.assertEqual(second.handled, ("register", "new"))

    async def test_nested_dispatch_restores_outer_event(self) -> None:
        inner = Router()
        inner.message("inner", AsyncMock())
        outer_event = event("/outer")

        async def handler(context):
            await inner.dispatch(self.bot, event("/inner"))
            self.assertIs(current_event(), outer_event)

        self.router.message("outer", handler)
        await self.router.dispatch(self.bot, outer_event)
        with self.assertRaises(RuntimeError):
            current_event()


if __name__ == "__main__":
    unittest.main()
