"""响应器定义复用、组合条件和事件执行作用域的行为回归。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import Mock, AsyncMock

from fxbot.core import (
    Rule,
    Event,
    State,
    Router,
    BotClient,
    Permission,
    MatchResult,
    HandlerContext,
    CommandArguments,
    on_command,
    on_message,
)
from fxbot.core.matcher import Matcher, MatcherOptions
from fxbot.core.plugins import LoadedPlugin, PluginContext, PluginRuntime, PluginMetadata
from fxbot.core.matching import command_match
from fxbot.core.contracts import ApplicationServices
from fxbot.core.scheduler import Scheduler
from fxbot.core.application import Application
from tests.migration.test_router import event


class MatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = Application()
        self.bot = SimpleNamespace(platform="test", bot_id="bot", send=AsyncMock())
        self.auth = SimpleNamespace(check=AsyncMock(return_value=True), declare_commands=lambda *args: None)
        self.auth.configuration = SimpleNamespace(subscribe=lambda callback: lambda: None)
        self.context = PluginContext(self.app, Scheduler(self.app), self.auth, None, self.auth.configuration)
        self.addAsyncCleanup(self.app.stop)

    async def install(self, *matchers):
        definition = LoadedPlugin("test", PluginMetadata("测试"), tuple(item.snapshot() for item in matchers), (), ())
        PluginRuntime(self.context, definition).install()
        await self.app.start()

    async def test_one_context_reaches_permission_rule_and_all_handlers(self):
        contexts = []

        async def permission(ctx):
            contexts.append(ctx)
            ctx.state["permission"] = True
            return True

        async def rule(ctx):
            contexts.append(ctx)
            self.assertTrue(ctx.state["permission"])
            return ctx.arguments == "argument"

        matching = Mock(wraps=command_match("ping"))
        matcher = Matcher(matching, MatcherOptions(name="ping", rule=Rule(rule), permission=Permission(permission)))

        @matcher.handle()
        async def first(ctx):
            contexts.append(ctx)
            ctx.state["value"] = "shared"

        @matcher.handle()
        async def second(ctx):
            contexts.append(ctx)
            await matcher.send(ctx.state["value"])

        await self.install(matcher)
        inbound = event("/ping argument")
        result = await self.app.router.dispatch(self.bot, inbound)
        matching.assert_called_once_with(inbound)
        self.assertEqual(result.handled, ("test:ping",))
        self.assertEqual(len(contexts), 4)
        self.assertTrue(all(ctx is contexts[0] for ctx in contexts))
        self.assertIs(contexts[0].bot, self.bot)
        self.assertEqual(self.bot.send.await_args.args[1].plain_text(), "shared")

    async def test_finish_ends_whole_handler_chain_but_respects_block_false(self):
        matcher = on_command("ping", block=False)
        after = AsyncMock()

        @matcher.handle()
        async def first():
            await matcher.finish("finished")

        @matcher.handle()
        async def second(ctx):
            await after()

        await self.install(matcher)
        self.app.router.message("fallback", after, priority=99)
        result = await self.app.router.dispatch(self.bot, event())
        self.assertEqual(result.handled, ("test:ping", "fallback"))
        after.assert_awaited_once()

    async def test_handlers_remain_sequential_when_sibling_matcher_runs_concurrently(self):
        first, sibling = on_command("ping", name="first"), on_command("ping", name="sibling")
        entered, release = asyncio.Event(), asyncio.Event()
        after = AsyncMock()

        @first.handle()
        async def prepare():
            entered.set()
            await release.wait()

        @first.handle()
        async def respond():
            await after()

        @sibling.handle()
        async def observer():
            await entered.wait()
            after.assert_not_awaited()
            release.set()

        await self.install(first, sibling)
        result = await asyncio.wait_for(self.app.router.dispatch(self.bot, event()), 1)
        self.assertEqual(result.handled, ("test:first", "test:sibling"))
        after.assert_awaited_once()

    async def test_global_permission_cannot_be_bypassed_by_custom_or(self):
        custom = AsyncMock(return_value=True)
        matcher = on_command("ping", permission=Permission(custom) | custom)

        @matcher.handle()
        async def handler():
            self.fail("统一权限拒绝时不能运行处理器")

        await self.install(matcher)
        self.auth.check.return_value = False
        result = await self.app.router.dispatch(self.bot, event())
        self.assertEqual(result.denied, ("test:ping",))
        custom.assert_not_awaited()

    async def test_composed_conditions_short_circuit_and_require_exact_bool(self):
        ctx = SimpleNamespace()
        yes, no, later = AsyncMock(return_value=True), AsyncMock(return_value=False), AsyncMock(return_value=True)
        self.assertFalse(await (Rule(yes) & no & later)(ctx))
        later.assert_not_awaited()
        self.assertTrue(await (Permission(no) | yes | later)(ctx))
        later.assert_not_awaited()
        self.assertFalse(await Rule(AsyncMock(return_value=1))(ctx))
        self.assertFalse(await Permission(AsyncMock(return_value=1))(ctx))

    async def test_concurrent_invocations_keep_targets_arguments_and_state_separate(self):
        ready = asyncio.Event()
        count = 0
        matcher = on_command("ping")

        @matcher.handle()
        async def handler(ctx):
            nonlocal count
            ctx.state["argument"] = ctx.arguments
            count += 1
            if count == 2:
                ready.set()
            await ready.wait()
            await matcher.finish(ctx.state["argument"])

        await self.install(matcher)
        first = event("/ping first")
        second = replace(event("/ping second"), target=replace(first.target, target_id="other"))
        await asyncio.gather(self.app.router.dispatch(self.bot, first), self.app.router.dispatch(self.bot, second))
        self.assertEqual(
            {(call.args[0].target_id, call.args[1].plain_text()) for call in self.bot.send.await_args_list},
            {(first.target.target_id, "first"), (second.target.target_id, "second")},
        )
        with self.assertRaises(RuntimeError):
            await matcher.send("outside")

    async def test_nested_dispatch_restores_outer_matcher(self):
        outer, inner = on_command("ping"), on_command("inner")

        @inner.handle()
        async def child():
            await inner.finish("inner")

        @outer.handle()
        async def parent(ctx):
            await self.app.router.dispatch(ctx.bot, event("/inner"))
            await outer.finish("outer")

        await self.install(outer, inner)
        await self.app.router.dispatch(self.bot, event())
        self.assertEqual([call.args[1].plain_text() for call in self.bot.send.await_args_list], ["inner", "outer"])

    async def test_inherited_context_expires_when_response_finishes(self):
        ready = asyncio.Event()
        child = None
        matcher = on_command("ping")

        async def deferred():
            await ready.wait()
            await matcher.send("stale")

        @matcher.handle()
        async def handler():
            nonlocal child
            child = asyncio.create_task(deferred())

        await self.install(matcher)
        await self.app.router.dispatch(self.bot, event())
        ready.set()
        with self.assertRaises(RuntimeError):
            await child
        self.bot.send.assert_not_awaited()

    async def test_exception_and_cancellation_restore_context(self):
        for error in (ValueError("business-error"), asyncio.CancelledError()):
            matcher = on_command("ping")

            @matcher.handle()
            async def handler():
                raise error

            definition = matcher.snapshot()
            router = Router()
            router.command("ping", "/ping", definition.run)
            if isinstance(error, asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await router.dispatch(self.bot, event())
            else:
                with self.assertLogs("fxbot.core.router", level="ERROR"):
                    self.assertEqual((await router.dispatch(self.bot, event())).failed, ("ping",))
            with self.assertRaises(RuntimeError):
                await matcher.send("outside")

    async def test_definitions_are_snapshotted_and_handlers_sealed(self):
        matcher = on_command("ping")

        @matcher.handle()
        async def handler(ctx):
            await matcher.finish(ctx.arguments)

        await self.install(matcher)
        with self.assertRaises(AttributeError):
            matcher.match = lambda value: None
        result = await self.app.router.dispatch(self.bot, event("/ping stable"))
        self.assertEqual(result.handled, ("test:ping",))
        with self.assertRaises(RuntimeError):
            matcher.handle()(handler)

    async def test_shared_definition_keeps_two_application_services_isolated(self):
        matcher = on_command("ping")
        captured = []

        def factory(runtime):
            return []

        @matcher.handle()
        async def handler(ctx):
            state = ctx.service(factory)
            state.append(ctx.arguments)
            captured.append((ctx.app_services.application, state))

        await self.install(matcher)
        other = Application()
        self.addAsyncCleanup(other.stop)
        context = PluginContext(other, Scheduler(other), self.auth, None, self.auth.configuration)
        definition = LoadedPlugin("test", PluginMetadata("测试"), (matcher.snapshot(),), (), ())
        PluginRuntime(context, definition).install()
        await other.start()
        await self.app.router.dispatch(self.bot, event("/ping first"))
        await other.router.dispatch(self.bot, event("/ping other"))
        await self.app.router.dispatch(self.bot, event("/ping last"))
        self.assertIs(captured[0][0], self.app)
        self.assertIs(captured[1][0], other)
        self.assertIs(captured[0][1], captured[2][1])
        self.assertIsNot(captured[0][1], captured[1][1])
        self.assertEqual(captured[0][1], ["first", "last"])
        self.assertEqual(captured[1][1], ["other"])

    async def test_invalid_handler_contracts_fail_at_registration(self):
        matcher = on_message()
        with self.assertRaises(TypeError):
            matcher.handle()(lambda: None)

        async def bad(first, second):
            pass

        with self.assertRaises(TypeError):
            matcher.handle()(bad)
        with self.assertRaises(ValueError):
            matcher.snapshot()
        with self.assertRaises(TypeError):
            on_message(when=lambda value: True)

    async def test_typed_parameters_are_bound_once_from_the_same_execution(self):
        matcher = on_command("ping")
        seen = []

        @matcher.handle()
        async def handler(
            ctx: HandlerContext,
            message: Event,
            bot: BotClient,
            state: State,
            arguments: CommandArguments,
            result: MatchResult,
            app: ApplicationServices,
            current: Matcher,
        ):
            self.assertIs(ctx.event, message)
            self.assertIs(ctx.bot, bot)
            self.assertIs(ctx.state, state)
            self.assertIs(ctx.match, result)
            self.assertIs(app, self.context)
            self.assertIs(current, matcher)
            self.assertIsInstance(arguments, CommandArguments)
            seen.append(arguments)

        await self.install(matcher)
        result = await self.app.router.dispatch(self.bot, event("/ping value"))
        self.assertFalse(result.failed)
        self.assertEqual(seen, ["value"])

    async def test_unknown_parameter_types_fail_during_declaration(self):
        matcher = on_command("ping")

        async def handler(value: str):
            pass

        with self.assertRaises(TypeError):
            matcher.handle()(handler)
        with self.assertRaises(TypeError):
            on_command("ping", priority="fast")

    async def test_options_are_immutable_and_regex_groups_are_available(self):
        from fxbot.core import on_regex

        matcher = on_regex(r"^hello (?P<name>\w+)$", name="hello")
        groups = []

        @matcher.handle()
        async def handler(result: MatchResult):
            groups.append((result.group(1), result.group("name")))

        with self.assertRaises(AttributeError):
            matcher.options.priority = 9
        await self.install(matcher)
        result = await self.app.router.dispatch(self.bot, event("hello world"))
        self.assertFalse(result.failed)
        self.assertEqual(groups, [("world", "world")])
