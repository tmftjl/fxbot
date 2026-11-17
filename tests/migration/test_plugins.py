"""插件元信息、声明路由、资源回滚和应用隔离的核心测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
import importlib
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

from fxbot.core import Event, Message, SessionTarget
from fxbot.core.plugins import PluginContext, load_plugins, discover_plugins
from fxbot.core.matching import MatchResult
from fxbot.core.services import ServiceScope
from fxbot.core.scheduler import Scheduler
from fxbot.core.application import Application, ApplicationState


class PluginRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.object(importlib.import_module("fxbot.plugins"), "__path__", [str(self.root)]))
        self.enterContext(patch("fxbot.core.plugins.files", return_value=self.root))
        self.addCleanup(self.clear_modules)
        self.app = Application()
        self.events = []
        self.auth = Mock()
        self.auth.check = AsyncMock(return_value=True)
        self.auth.guard = lambda feature: AsyncMock(return_value=True)
        self.context = PluginContext(self.app, Scheduler(self.app), self.auth, self.events, self.auth.configuration)

        async def cleanup():
            if self.app.state is not ApplicationState.STOPPED:
                await self.app.stop()

        self.addAsyncCleanup(cleanup)

    def clear_modules(self):
        for name in tuple(sys.modules):
            if name.startswith("fxbot.plugins.registry_fixture_"):
                del sys.modules[name]
        importlib.invalidate_caches()

    def plugin(self, folder="registry_fixture_sample", body=""):
        path = self.root / folder
        path.mkdir()
        (path / "__init__.py").write_text(
            (
                "from fxbot.core.plugin import PluginMetadata, on_command, on_regex, on_event, on_mess"
                "age, Service\n"
                "from fxbot.core.plugin import scheduled\n"
                "__plugin_meta__ = PluginMetadata(name='测试插件', description='描述', usage='#你好')\n"
            )
            + body,
            encoding="utf-8",
        )
        return path

    async def test_automatic_import_collects_chinese_metadata_and_permissions(self):
        path = self.plugin(
            body=(
                "hello_matcher = on_command('你好', name='hello', aliases={'嗨'}, level='admin', scene='g"
                "roup', display_name='问候')\n"
                "\n"
                "\n"
                "@hello_matcher.handle()\n"
                "async def hello(context): await context.send(context.arguments)\n"
            )
        )
        plugins = discover_plugins()
        self.assertEqual([item.name for item in plugins], [path.name])
        load_plugins(self.context, plugins)
        await self.app.start()
        self.assertEqual(self.app.plugin_metadata[path.name].name, "测试插件")
        self.assertEqual(self.app.command_metadata[path.name], {"hello": "问候"})
        self.assertEqual(self.auth.declare_commands.call_args.args[1][0].level, "admin")
        bot = SimpleNamespace(send=AsyncMock(), platform="test", bot_id="bot")
        event = Event(
            None,
            None,
            "message",
            "test",
            "bot",
            "user",
            "用户",
            SessionTarget("test", "bot", "group", "g"),
            Message.text("#嗨 参数"),
        )
        result = await self.app.router.dispatch(bot, event)
        self.assertEqual(result.handled, (path.name + ":hello",))
        self.assertEqual(bot.send.await_args.args[1].plain_text(), "参数")
        denied = await self.app.router.dispatch(
            bot,
            Event(
                None,
                None,
                "message",
                "test",
                "bot",
                "user",
                "",
                SessionTarget("test", "bot", "private", "user"),
                Message.text("#嗨 参数"),
            ),
        )
        self.assertFalse(denied.handled)

    async def test_no_json_setup_plugin_object_or_install_needed(self):
        self.plugin(
            body="hello_matcher = on_command('你好')\n\n\n@hello_matcher.handle()\nasync def hello(context): pass\n"
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        self.assertEqual(len(self.app.router._routes), 1)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 0)

    async def test_plugin_import_failure_is_skipped_without_blocking_core(self):
        self.plugin(folder="registry_fixture_broken", body="import definitely_missing_dependency\n")
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        self.assertEqual(self.app.plugin_metadata, {})

    async def test_services_are_lazy_shared_per_app_and_closed_in_reverse_order(self):
        path = self.plugin(
            body=(
                "class Owned(Service):\n"
                "    def __init__(self, runtime, name): self.runtime, self.name = runtime, name\n"
                "    async def close(self): self.runtime.notifications.append('close-' + self.name)\n"
                "def first(runtime):\n"
                "    runtime.notifications.append('first')\n"
                "    return Owned(runtime, 'first')\n"
                "def second(runtime):\n"
                "    runtime.notifications.append('second')\n"
                "    return Owned(runtime, 'second')\n"
                "hello_matcher = on_command('你好')\n"
                "\n"
                "\n"
                "@hello_matcher.handle()\n"
                "async def hello(context):\n"
                "    assert context.service(first) is context.service(first)\n"
                "    context.service(second)\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        self.assertEqual(self.events, [])
        module = sys.modules["fxbot.plugins." + path.name]
        from fxbot.core.router import HandlerContext

        scope = ServiceScope()
        ctx = HandlerContext(None, None, MatchResult(""), scope, self.context)
        first = ctx.service(module.first)
        self.assertIs(first, ctx.service(module.first))
        ctx.service(module.second)
        # Real router invocation exercises the framework-owned scope and disposal.
        bot = SimpleNamespace(platform="test", bot_id="bot")
        event = Event(
            None,
            None,
            "message",
            "test",
            "bot",
            "user",
            "",
            SessionTarget("test", "bot", "private", "user"),
            Message.text("#你好"),
        )
        await self.app.router.dispatch(bot, event)
        await self.app.stop()
        self.assertEqual(self.events[-2:], ["close-second", "close-first"])
        with self.assertRaises(RuntimeError):
            ctx.service(module.first)

    async def test_service_scopes_do_not_cross_applications(self):
        self.plugin(
            body=(
                "def counter(runtime): return []\n"
                "@scheduled(interval=60)\n"
                "async def check(context): context.service(counter).append(context)\n"
            )
        )
        definitions = discover_plugins()
        load_plugins(self.context, definitions)
        other = Application()
        other_context = PluginContext(other, Scheduler(other), self.auth, [], self.auth.configuration)
        load_plugins(other_context, definitions)
        await self.app.start()
        await other.start()
        try:
            (first,) = self.context.scheduler.jobs
            (second,) = other_context.scheduler.jobs
            await first.callback()
            await second.callback()
            from fxbot.plugins.registry_fixture_sample import counter

            left = first.callback.__self__.context
            right = second.callback.__self__.context
            self.assertIsNot(left.service(counter), right.service(counter))
        finally:
            await other.stop()

    async def test_service_close_failure_still_releases_other_services(self):
        self.plugin(
            body=(
                "class Owned(Service):\n"
                "    def __init__(self, runtime): self.runtime = runtime\n"
                "    async def close(self): self.runtime.notifications.append('closed')\n"
                "class Broken(Service):\n"
                "    async def close(self): raise RuntimeError('failed-close')\n"
                "def first(runtime): return Owned(runtime)\n"
                "def second(runtime): return Broken()\n"
                "hello_matcher = on_command('hello')\n"
                "\n"
                "\n"
                "@hello_matcher.handle()\n"
                "async def hello(ctx):\n"
                "    ctx.service(first)\n"
                "    ctx.service(second)\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        bot = SimpleNamespace(platform="test", bot_id="bot")
        event = Event(
            None,
            None,
            "message",
            "test",
            "bot",
            "user",
            "",
            SessionTarget("test", "bot", "private", "user"),
            Message.text("#hello"),
        )
        await self.app.router.dispatch(bot, event)
        with self.assertRaisesRegex(RuntimeError, "failed-close"):
            await self.app.stop()
        self.assertEqual(self.events, ["closed"])

    async def test_dynamic_plan_subscribes_reconfigures_and_unsubscribes(self):
        self.plugin(
            body=(
                "from fxbot.core.scheduler import Interval\n"
                "def plan(ctx):\n"
                "    seconds = ctx.notifications[0]\n"
                "    return Interval(seconds) if seconds else None\n"
                "@scheduled(plan=plan, watch_config=True)\n"
                "async def check(ctx): pass\n"
            )
        )
        self.events.append(10)
        unsubscribe = Mock()
        self.auth.configuration.subscribe.return_value = unsubscribe
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        (job,) = self.context.scheduler.jobs
        self.assertEqual(job.schedule.seconds, 10)
        reconfigure = self.auth.configuration.subscribe.call_args.args[0]
        self.events[0] = 20
        reconfigure()
        self.assertEqual(self.context.scheduler.jobs[0].schedule.seconds, 20)
        self.events[0] = 0
        reconfigure()
        self.assertFalse(self.context.scheduler.jobs)
        await self.app.stop()
        unsubscribe.assert_called_once()

    async def test_outgoing_transform_installed_once_and_removed_on_stop(self):
        self.plugin(
            body=(
                "from fxbot.core.plugin import outgoing\n"
                "@outgoing\n"
                "def transform(bot, target, message): return message\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        self.assertEqual(len(self.app._message_transforms), 1)
        await self.app.stop()
        self.assertFalse(self.app._message_transforms)

    async def test_failed_config_subscription_rolls_back_registered_job(self):
        self.plugin(body=("@scheduled(interval=60, watch_config=True)\nasync def check(ctx): pass\n"))
        self.auth.configuration.subscribe.side_effect = RuntimeError("subscription-failed")
        load_plugins(self.context, discover_plugins())
        with self.assertRaisesRegex(RuntimeError, "subscription-failed"):
            await self.app.start()
        self.assertFalse(self.context.scheduler.jobs)

    async def test_import_failure_is_not_swallowed(self):
        self.plugin(body="raise RuntimeError('plugin failed')\n")
        with self.assertRaisesRegex(RuntimeError, "plugin failed"):
            load_plugins(self.context, discover_plugins())

    async def test_invalid_later_plugin_does_not_install_earlier_routes(self):
        self.plugin(
            "registry_fixture_a",
            body="hello = on_command('你好')\n@hello.handle()\nasync def answer(ctx): pass\n",
        )
        self.plugin("registry_fixture_z", body="raise RuntimeError('invalid-later-plugin')\n")
        with self.assertRaisesRegex(RuntimeError, "invalid-later-plugin"):
            load_plugins(self.context, discover_plugins())
        self.assertEqual(self.app.router._routes, [])
        self.assertEqual(self.app.plugin_metadata, {})

    async def test_route_collision_is_validated_before_any_plugin_is_installed(self):
        self.plugin(
            "registry_fixture_a",
            body="m = on_command('hello', name='hello')\n@m.handle()\nasync def handle(ctx): pass\n",
        )
        self.plugin(
            "registry_fixture_z",
            body="m = on_command('other', name='other')\n@m.handle()\nasync def handle(ctx): pass\n",
        )
        self.app.router.message("registry_fixture_z:other", AsyncMock())
        before = list(self.app.router._routes)
        with self.assertRaises(ValueError):
            load_plugins(self.context, discover_plugins())
        self.assertEqual(self.app.router._routes, before)
        self.assertFalse(self.app.plugin_metadata)
        self.assertFalse(self.app._starters)

    async def test_business_completed_key_does_not_control_framework_first_run(self):
        self.plugin(
            body=(
                "@scheduled(interval=60)\n"
                "async def check(ctx):\n"
                "    ctx.state['completed'] = False\n"
                "    ctx.notifications.append(ctx.first_run)\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        (job,) = self.context.scheduler.jobs
        await job.callback()
        await job.callback()
        self.assertEqual(self.events, [True, False])

    async def test_duplicate_plugin_names_fail_before_import(self):
        self.plugin()
        definitions = discover_plugins()
        with self.assertRaises(ValueError):
            load_plugins(self.context, (*definitions, *definitions))
        self.assertNotIn(definitions[0].module, sys.modules)

    async def test_same_matcher_reexport_does_not_duplicate_routes(self):
        self.plugin(
            body=(
                "hello_matcher = on_command('你好')\n"
                "\n"
                "\n"
                "@hello_matcher.handle()\n"
                "async def hello(context): pass\n"
                "hello_alias = hello_matcher\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        self.assertEqual(len(self.app.router._routes), 1)

    async def test_duplicate_command_names_fail(self):
        self.plugin(
            body=(
                "first_matcher = on_command('你好', name='same')\n"
                "\n"
                "\n"
                "@first_matcher.handle()\n"
                "async def first(context): pass\n"
                "second_matcher = on_regex('other', name='same')\n"
                "\n"
                "\n"
                "@second_matcher.handle()\n"
                "async def second(context): pass\n"
            )
        )
        with self.assertRaises(ValueError):
            load_plugins(self.context, discover_plugins())

    async def test_private_helpers_not_discovered(self):
        self.plugin("_helper", body="raise AssertionError('helper')")
        (self.root / "resources").mkdir()
        self.assertEqual(discover_plugins(), ())

    async def test_scheduled_declaration_registers_and_removes_job(self):
        self.plugin(
            body=(
                "@scheduled(interval=60, run_on_start=True)\n"
                "async def check(context): context.state['runs'] = context.state.get('runs', 0) + 1\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        (job,) = self.context.scheduler.jobs
        self.assertEqual(job.name, "registry_fixture_sample:check")
        self.assertTrue(job.immediate)
        await job.callback()
        await job.callback()
        await self.app.stop()
        self.assertFalse(self.context.scheduler.jobs)

    async def test_scheduled_first_run_only_changes_after_success(self):
        self.plugin(
            body=(
                "@scheduled(interval=60, run_on_start=True)\n"
                "async def check(context):\n"
                "    context.notifications.append(context.first_run)\n"
                "    if len(context.notifications) == 1: raise ValueError('fake')\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        (job,) = self.context.scheduler.jobs
        with self.assertRaises(ValueError):
            await job.callback()
        await job.callback()
        await job.callback()
        self.assertEqual(self.events, [True, True, False])

    async def test_scheduled_job_cancellation_waits_for_cleanup(self):
        import asyncio

        self.plugin(
            body=(
                "import asyncio\n"
                "@scheduled(interval=60, run_on_start=True)\n"
                "async def check(context):\n"
                "    context.notifications.append('entered')\n"
                "    try: await asyncio.Event().wait()\n"
                "    finally:\n"
                "        await asyncio.sleep(0)\n"
                "        context.notifications.append('closed')\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        task = self.app.spawn(self.context.scheduler.run(), name="scheduler")
        for _ in range(30):
            if self.events:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.events, ["entered"])
        await self.app.stop()
        self.assertEqual(self.events, ["entered", "closed"])
        self.assertTrue(task.done())
        self.assertFalse(self.context.scheduler.jobs)

    async def test_missing_metadata_is_an_error(self):
        path = self.plugin()
        (path / "__init__.py").write_text("", encoding="utf-8")
        with self.assertRaises(TypeError):
            load_plugins(self.context, discover_plugins())

    async def test_live_application_cannot_load_plugins(self):
        await self.app.start()
        with self.assertRaises(RuntimeError):
            load_plugins(self.context, ())

    async def test_wrong_scheduler_is_rejected(self):
        with self.assertRaises(ValueError):
            load_plugins(
                PluginContext(self.app, Scheduler(Application()), self.auth, None, self.auth.configuration), ()
            )

    async def test_events_and_conditional_messages_do_not_block_unmatched_input(self):
        self.plugin(
            body=(
                "async def only_yes(context): return context.arguments == 'yes'\n"
                "answer_matcher = on_message(name='answer', rule=only_yes)\n"
                "\n"
                "\n"
                "@answer_matcher.handle()\n"
                "async def answer(context): await context.send('matched')\n"
                "joined_matcher = on_event('member_joined', name='joined')\n"
                "\n"
                "\n"
                "@joined_matcher.handle()\n"
                "async def joined(context): await context.send('welcome')\n"
            )
        )
        load_plugins(self.context, discover_plugins())
        await self.app.start()
        bot = SimpleNamespace(send=AsyncMock(), platform="test", bot_id="bot", message_transform=None)
        self.app.register(bot, close=AsyncMock())
        event = Event(
            None,
            None,
            "message",
            "test",
            "bot",
            "user",
            "",
            SessionTarget("test", "bot", "group", "g"),
            Message.text("no"),
        )
        self.assertFalse((await self.app.router.dispatch(bot, event)).blocked)
        from dataclasses import replace

        result = await self.app.router.dispatch(bot, replace(event, message=Message.text("yes")))
        self.assertTrue(result.blocked)
        notice = await self.app.router.dispatch(bot, replace(event, event_type="member_joined"))
        self.assertEqual(notice.handled, ("registry_fixture_sample:joined",))
