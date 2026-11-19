"""开放聊天接口的模型、工具、托管执行与调度行为，不连接外部服务。"""

from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from fxbot.chat import hooks
from fxbot.config import SYSTEM_DEFAULTS
from fxbot.chat.types import ChatRequest, ChatResponse
from fxbot.chat.context import ChatContext, ChatExpired, current_chat
from fxbot.chat.service import ChatService
from fxbot.chat.session import ChatSessionStore
from fxbot.chat.providers import LLMRequest, LLMResponse
from fxbot.chat.extensions import ChatExtension
from fxbot.chat.scheduling import ChatScheduling, chat_scheduling
from fxbot.chat.tools.types import ToolSpec
from fxbot.chat.tools.runtime import ToolRuntime
from fxbot.chat.tools.registry import ToolRegistry
from fxbot.chat.providers.manager import provider_manager


def call(name="lookup", arguments='{"query":"x"}'):
    return {"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}}


class ExtensionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chat_scheduling.start()
        self.config = deepcopy(SYSTEM_DEFAULTS)
        self.config["chat"].update(enabled=True, rest_time_no_reply=False, max_private_context_size=30)
        from fxbot.config import get_manager

        self.enterContext(patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(self.config)))
        self.enterContext(patch("fxbot.chat.memory.state.is_ready", return_value=False))
        self.store = ChatSessionStore()
        self.service = ChatService(self.store)
        self.request = ChatRequest("test:bot:private:user", "user", None, "hi", "turn", "d1", "private")
        self.request.accepted_epoch = 0
        self.runtime = ToolRuntime(sender=SimpleNamespace(send=AsyncMock(return_value={"message_id": "sent"})))
        self.ctx = ChatContext(self.service, self.request, self.runtime)
        self.extension = ChatExtension("test-open-api")
        self.addCleanup(self.extension.close)
        self.provider = SimpleNamespace(
            provider_id="test",
            get_model=lambda: "model",
            chat=AsyncMock(return_value=LLMResponse(content="answer")),
        )
        self.leases = []

        @asynccontextmanager
        async def lease(name):
            self.leases.append(("open", name))
            try:
                yield self.provider
            finally:
                self.leases.append(("close", name))

        self.enterContext(patch.object(provider_manager, "lease", lease))
        self.enterContext(patch("fxbot.chat.context.current_chat", current_chat))

    async def asyncTearDown(self):
        await chat_scheduling.stop()

    def registry(self, handler=None, permission=""):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "lookup",
                "lookup",
                {"type": "object", "properties": {"query": {"type": "string"}}},
                handler or AsyncMock(return_value={"found": True}),
                permission=permission,
            )
        )
        return registry

    async def test_single_call_has_no_tool_execution_history_or_sending(self):
        self.provider.chat.return_value = LLMResponse(tool_calls=[call()])
        result = await self.ctx.llm_generate(
            LLMRequest(
                messages=[{"role": "user", "content": "x"}],
                provider_id="test",
            )
        )
        self.assertTrue(result.has_tool_calls)
        self.assertEqual(self.store.get(self.ctx.session_id), [])
        self.runtime.sender.send.assert_not_awaited()
        self.assertEqual(self.leases, [("open", "test"), ("close", "test")])

    async def test_mutable_request_and_response_hooks(self):
        def before(ctx, request):
            request.messages = [{"role": "system", "content": "custom"}]
            request.model, request.temperature, request.max_tokens = "other", 0.1, 12

        def after(ctx, request, response):
            response.content = "changed"

        self.extension.on("llm_request", before)
        self.extension.on("llm_response", after)
        result = await self.ctx.llm_generate(LLMRequest(provider_id="test"))
        self.assertEqual(result.content, "changed")
        self.assertEqual(self.provider.chat.await_args.kwargs["messages"], [{"role": "system", "content": "custom"}])
        self.assertEqual(self.provider.chat.await_args.kwargs["model"], "other")

    async def test_provider_can_change_without_mutating_shared_provider(self):
        self.extension.on("llm_request", lambda ctx, req: setattr(req, "provider_id", "second"))
        await self.ctx.llm_generate(LLMRequest(provider_id="test"))
        self.assertIn(("open", "second"), self.leases)
        self.assertEqual(self.provider.provider_id, "test")
        self.assertEqual(len(self.leases), 4)

    async def test_empty_provider_id_does_not_redirect_forever(self):
        await self.ctx.llm_generate(LLMRequest(provider_id=""))
        self.assertEqual(len(self.leases), 2)

    async def test_hook_can_call_model_without_recursive_same_hook(self):
        entries = []

        async def before(ctx, request):
            entries.append(True)
            await ctx.llm_generate(LLMRequest(provider_id="test", use_hooks=False))

        self.extension.on("llm_request", before)
        await self.ctx.llm_generate(LLMRequest(provider_id="test"))
        self.assertEqual(entries, [True])
        self.assertEqual(self.provider.chat.await_count, 2)

    async def test_tools_loop_uses_local_registry_and_backfills_results(self):
        handler = AsyncMock(return_value={"found": True})
        self.provider.chat.side_effect = [LLMResponse(tool_calls=[call()]), LLMResponse(content="done")]
        request = LLMRequest(
            messages=[{"role": "user", "content": "x"}],
            provider_id="test",
            tool_registry=self.registry(handler),
        )
        result = await self.ctx.tool_loop_agent(request)
        self.assertEqual(result.content, "done")
        handler.assert_awaited_once()
        backfill = self.provider.chat.await_args.kwargs["messages"][-1]
        self.assertEqual(backfill["role"], "tool")
        self.assertEqual(backfill["tool_call_id"], "call-1")
        self.assertEqual(len(request.messages), 1)
        self.assertEqual(self.store.get(self.ctx.session_id), [])

    async def test_dynamic_tool_permission_is_rechecked(self):
        handler = AsyncMock()
        result = await self.ctx.run_tool("lookup", {}, registry=self.registry(handler, "plugin:restricted"))
        self.assertFalse(result["ok"])
        handler.assert_not_awaited()

    async def test_tool_hooks_modify_arguments_and_results(self):
        handler = AsyncMock(return_value="original")
        self.extension.on("tool_start", lambda ctx, spec, args: args.update(query="changed"))
        self.extension.on("tool_end", lambda ctx, spec, args, result: result.update(result="changed"))
        result = await self.ctx.run_tool("lookup", {"query": "x"}, registry=self.registry(handler))
        self.assertEqual(handler.await_args.kwargs["query"], "changed")
        self.assertEqual(result["result"], "changed")

    async def test_tool_guard_denial(self):
        handler = AsyncMock()
        self.extension.on("tool_guard", lambda *args: False)
        result = await self.ctx.run_tool("lookup", {}, registry=self.registry(handler))
        self.assertFalse(result["ok"])
        handler.assert_not_awaited()

    async def test_tool_loop_budget_does_not_retry_side_effects(self):
        handler = AsyncMock(return_value="ok")
        self.provider.chat.return_value = LLMResponse(tool_calls=[call()])
        with self.assertRaisesRegex(RuntimeError, "上限"):
            await self.ctx.tool_loop_agent(
                LLMRequest(
                    provider_id="test",
                    tool_registry=self.registry(handler),
                    max_steps=1,
                )
            )
        self.assertEqual(handler.await_count, 1)

    async def test_timeout_releases_provider(self):
        async def wait(**kwargs):
            await asyncio.Event().wait()

        self.provider.chat.side_effect = wait
        with self.assertRaises(TimeoutError):
            await self.ctx.llm_generate(LLMRequest(provider_id="test", timeout=0.01))
        self.assertEqual(self.leases[-1], ("close", "test"))

    async def test_clear_expires_model_and_tool_calls(self):
        self.store.clear(self.ctx.session_id)
        with self.assertRaises(ChatExpired):
            await self.ctx.llm_generate(LLMRequest(provider_id="test"))
        with self.assertRaises(ChatExpired):
            await self.ctx.run_tool("lookup", {}, registry=self.registry())
        self.provider.chat.assert_not_awaited()

    async def test_history_excludes_internal_unsent_and_current_message(self):
        for item in [
            {"role": "user", "content": "older", "_sender_id": "user"},
            {"role": "assistant", "content": "visible", "_self_message": True},
            {"role": "assistant", "content": "unsent"},
            {"role": "user", "content": "private", "_sender_id": "user", "_reference": True},
            {"role": "user", "content": "hi", "_sender_id": "user", "_source_ids": ["d1"]},
        ]:
            self.store.append(self.ctx.session_id, item)
        self.assertEqual([item["content"] for item in self.ctx.history()], ["older", "visible"])

    async def test_chat_hook_replaces_planner_and_standard_send_records_visible_history(self):
        self.extension.on("chat_request", lambda ctx: ctx.set_result("plugin"))
        self.extension.on("reply", lambda ctx, result: setattr(result, "text", "filtered"))
        with patch.object(self.service, "_process", AsyncMock()) as default:
            sent = await self.service.process_and_send(self.request, self.runtime)
        self.assertTrue(sent)
        default.assert_not_awaited()
        self.assertEqual(self.runtime.sender.send.await_args.args[0], "filtered")
        self.assertEqual([item["role"] for item in self.store.get(self.ctx.session_id)], ["user", "assistant"])

    async def test_none_hook_continues_default_and_stop_is_explicit(self):
        self.extension.on("chat_request", lambda ctx: None)
        with patch.object(self.service, "_process", AsyncMock(return_value=ChatResponse("default"))) as default:
            response = await self.service.process(self.request, self.runtime)
        self.assertEqual(response.text, "default")
        default.assert_awaited_once()
        self.extension.on("chat_request", lambda ctx: ctx.stop_event())
        with patch.object(self.service, "_process", AsyncMock()) as default:
            response = await self.service.process(self.request, self.runtime)
        default.assert_not_awaited()
        self.assertEqual(response.text, "")

    async def test_chat_request_error_fails_closed(self):
        self.extension.on("chat_request", AsyncMock(side_effect=ValueError("broken")))
        with patch.object(self.service, "_process", AsyncMock()) as default:
            with self.assertRaises(hooks.ChatHookError):
                await self.service.process(self.request, self.runtime)
        default.assert_not_awaited()
        self.assertIsNone(current_chat.get())
        self.assertEqual(self.service.running(self.ctx.session_id), ())

    async def test_managed_model_request_does_not_add_persona(self):
        await self.ctx.request_llm(LLMRequest(messages=[{"role": "user", "content": "only"}], provider_id="test"))
        self.assertEqual(self.provider.chat.await_args.kwargs["messages"], [{"role": "user", "content": "only"}])
        self.runtime.sender.send.assert_awaited_once()

    async def test_progress_does_not_write_final_history_and_guard_applies(self):
        await self.ctx.send("working")
        self.assertEqual(self.store.get(self.ctx.session_id), [])
        self.extension.on("send_guard", lambda *args: False)
        await self.ctx.send("blocked")
        self.assertEqual(self.runtime.sender.send.await_count, 1)

    async def test_final_send_failure_keeps_assistant_out_of_visible_history(self):
        self.runtime.sender.send.side_effect = OSError("offline")
        await self.ctx.submit("generated")
        self.assertEqual([item["role"] for item in self.store.get(self.ctx.session_id)], ["user"])

    async def test_same_final_result_cannot_be_committed_twice(self):
        await self.ctx.submit("once")
        with self.assertRaisesRegex(RuntimeError, "已提交"):
            await self.ctx.submit("twice")
        self.assertEqual(self.runtime.sender.send.await_count, 1)

    async def test_prepare_request_can_disable_every_enhancement(self):
        with patch.object(self.ctx, "recall", AsyncMock()) as memory:
            request = await self.ctx.prepare_request(
                persona=False,
                memory=False,
                expressions=False,
                summary=False,
                vision=False,
            )
        memory.assert_not_awaited()
        self.assertEqual(request.messages, [{"role": "user", "content": "hi"}])
        self.assertEqual(request.system_prompt, "")

    async def test_non_planner_execution_does_not_swallow_followup(self):
        self.service._active_requests[self.ctx.session_id] = self.request
        self.assertFalse(await self.service.queue_message(self.request, self.runtime))

    async def test_registration_rolls_back_when_plugin_import_fails(self):
        from fxbot.core.registration import registration_scope

        with self.assertRaises(ImportError):
            with registration_scope():
                hooks.register_chat_hook("request_guard", "failed-plugin", lambda *args: False)
                hooks.register_chat_hook("prompt", "failed-plugin", lambda *args: None)
        self.assertNotIn("failed-plugin", hooks._hooks["request_guard"])

    async def test_failed_import_restores_replaced_hooks_and_tools(self):
        from fxbot.core.registration import registration_scope

        def original(*args):
            return True

        remove = hooks.register_chat_hook("request_guard", "shared-name", original)
        self.addCleanup(remove)
        registry = self.registry()
        original_tool = registry.get("lookup")
        with self.assertRaises(ImportError):
            with registration_scope():
                hooks.register_chat_hook("request_guard", "shared-name", lambda *args: False)
                registry.register(ToolSpec("lookup", "replacement", {}, AsyncMock()))
                raise ImportError("missing API")
        self.assertIs(hooks._hooks["request_guard"]["shared-name"].callback, original)
        self.assertIs(registry.get("lookup"), original_tool)

    async def test_scheduling_owner_conflict_release_and_no_default_queue(self):
        scheduling = ChatScheduling()
        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await scheduling.claim("one", self.ctx, AsyncMock())
            with self.assertRaises(ValueError):
                await scheduling.claim("two", self.ctx, AsyncMock())
            await scheduling.release("one", self.ctx.session_id)
        self.assertIsNone(scheduling.owner(self.ctx.session_id))

    async def test_extension_close_releases_session_owner(self):
        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await self.extension.claim_session(self.ctx, AsyncMock())
        self.assertIsNotNone(chat_scheduling.owner(self.ctx.session_id))
        self.extension.close()
        self.assertIsNone(chat_scheduling.owner(self.ctx.session_id))

    async def test_cancel_running_execution_propagates_and_cleans_context(self):
        entered = asyncio.Event()
        states = []

        async def wait(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.provider.chat.side_effect = wait
        self.extension.on("chat_done", lambda ctx: states.append(ctx.status))
        task = asyncio.create_task(self.ctx.request_llm(LLMRequest(provider_id="test")))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(len(self.service.running(self.ctx.session_id)), 1)
        self.service.cancel(self.ctx.session_id)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(states, ["cancelled"])
        self.assertEqual(self.service.running(self.ctx.session_id), ())
        self.assertEqual(self.leases[-1], ("close", "test"))

    async def test_clear_during_generation_discards_old_result(self):
        async def clear(**kwargs):
            self.store.clear(self.ctx.session_id)
            return LLMResponse(content="stale")

        self.provider.chat.side_effect = clear
        response = await self.ctx.request_llm(LLMRequest(provider_id="test"))
        self.assertEqual(response.text, "")
        self.runtime.sender.send.assert_not_awaited()
        self.assertEqual(self.store.get(self.ctx.session_id), [])

    async def test_final_reply_parts_are_the_persisted_and_sent_content(self):
        self.extension.on("reply", lambda ctx, result: result.metadata.update(reply_parts=["one", "two"]))
        with patch("fxbot.chat.message_receive.sender.typing_time", return_value=0):
            response = await self.ctx.submit("original")
        self.assertEqual(response.text, "onetwo")
        self.assertEqual([c.args[0] for c in self.runtime.sender.send.await_args_list], ["one", "two"])
        self.assertEqual([m["content"] for m in self.store.get(self.ctx.session_id)][1:], ["one", "two"])

    async def test_after_send_runs_only_after_actual_success(self):
        seen = []
        self.extension.on("after_send", lambda ctx, message, receipt: seen.append((message, receipt["message_id"])))
        await self.ctx.submit("answer")
        self.assertEqual(seen, [("answer", "sent")])

    async def test_chat_hook_can_await_managed_model_without_reentering_lock(self):
        async def controller(ctx):
            response = await ctx.request_llm(LLMRequest(provider_id="test"))
            self.assertEqual(response.text, "answer")
            ctx.stop_event()

        self.extension.on("chat_request", controller)
        with patch.object(self.service, "_process", AsyncMock()) as default:
            await asyncio.wait_for(self.service.process_and_send(self.request, self.runtime), 1)
        default.assert_not_awaited()
        self.assertEqual(self.provider.chat.await_count, 1)
        self.assertEqual(self.runtime.sender.send.await_count, 1)

    async def test_executor_can_submit_without_returning_a_result(self):
        async def execute(ctx):
            await ctx.submit("submitted")

        self.extension.on("chat_request", lambda ctx: setattr(ctx, "executor", execute))
        await self.service.process_and_send(self.request, self.runtime)
        self.assertEqual(self.runtime.sender.send.await_args.args[0], "submitted")

    async def test_default_reply_can_use_hook_injected_dynamic_tools(self):
        from fxbot.chat.replyer import generator
        from fxbot.chat.providers.routing import TaskProvider

        registry = self.registry()
        self.extension.on("llm_request", lambda ctx, request: setattr(request, "tool_registry", registry))
        self.provider.chat.side_effect = [LLMResponse(tool_calls=[call()]), LLMResponse(content="done")]
        token = current_chat.set(self.ctx)
        try:
            async with TaskProvider(["test"], task="reply") as provider:
                response = await generator._generate(
                    self.service,
                    self.request,
                    self.runtime,
                    provider,
                    [{"role": "user", "content": "x"}],
                    None,
                    set(),
                    0,
                    30,
                )
        finally:
            current_chat.reset(token)
        self.assertEqual(response.content, "done")
        registry.get("lookup").handler.assert_awaited_once()

    async def test_memory_query_is_forwarded_to_existing_search_service(self):
        with patch("fxbot.chat.amemorix.service.search", AsyncMock(return_value={"hits": []})) as search:
            await self.ctx.recall("specific query")
        self.assertEqual(search.await_args.args, (self.ctx.session_id, "specific query"))

    async def test_new_active_turn_does_not_fabricate_user_history(self):
        self.extension.on("chat_request", lambda ctx: None)
        wake = self.ctx.new_turn("system task")
        await wake.submit("proactive answer")
        self.assertNotEqual(wake.request.turn_id, self.request.turn_id)
        self.assertEqual([m["role"] for m in self.store.get(self.ctx.session_id)], ["assistant"])

    async def test_observation_happens_even_without_default_provider_or_reply_trigger(self):
        from fxbot.chat import router
        from fxbot.core.application import Application
        from tests.migration.test_router import event

        app = Application()
        bot = SimpleNamespace(platform="test", bot_id="bot", send=AsyncMock())
        authorizer = SimpleNamespace(check=AsyncMock(return_value=True))
        observed = []
        self.extension.on("message", lambda ctx: observed.append(ctx.text))
        router.install(app, authorizer)
        with (
            patch.object(router, "adapt_message_event", AsyncMock(return_value=self.request)),
            patch.object(router.provider_manager, "has_provider", return_value=False),
            patch.object(router, "_handle_context", AsyncMock()),
        ):
            await app.router.dispatch(bot, event("ordinary message"))
        self.assertEqual(observed, ["hi"])

    async def test_scheduler_owner_receives_instead_of_default_focus_and_normal_paths(self):
        from fxbot.chat import router
        from fxbot.core.application import Application
        from tests.migration.test_router import event

        app = Application()
        bot = SimpleNamespace(platform="test", bot_id="bot", send=AsyncMock())
        receive = AsyncMock()
        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await self.extension.claim_session(self.ctx, receive)
        router.install(app, SimpleNamespace(check=AsyncMock(return_value=True)))
        with (
            patch.object(router, "adapt_message_event", AsyncMock(return_value=self.request)),
            patch.object(router, "_handle_context", AsyncMock()) as record,
            patch.object(router, "_handle_ai_fallback", AsyncMock()) as default,
        ):
            result = await app.router.dispatch(bot, event("ordinary message"))
        self.assertTrue(result.blocked)
        receive.assert_awaited_once()
        record.assert_not_awaited()
        default.assert_not_awaited()

    async def test_gate_can_force_non_mentioned_group_message(self):
        from dataclasses import replace

        from fxbot.chat import router
        from fxbot.core import SessionTarget
        from fxbot.chat.scheduling import GateDecision
        from tests.migration.test_router import event

        incoming = replace(event("ordinary"), target=SessionTarget("test", "bot", "group", "group"))
        incoming.metadata["_chat_gate"] = GateDecision(force=True)
        with (
            patch.object(router.provider_manager, "has_provider", return_value=True),
            patch.object(router, "_ingress_allowed", AsyncMock(return_value=True)),
        ):
            self.assertTrue(await router._ai_fallback_rule(SimpleNamespace(platform="test", bot_id="bot"), incoming))
            incoming.metadata["_chat_gate"].allow = False
            self.assertFalse(await router._ai_fallback_rule(SimpleNamespace(platform="test", bot_id="bot"), incoming))

    async def test_owned_background_flow_is_cancelled_on_extension_close(self):
        from fxbot.core.application import Application

        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        self.runtime.application = app
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def flow():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await self.extension.claim_session(self.ctx, AsyncMock())
        task = chat_scheduling.spawn(self.ctx, flow)
        await asyncio.wait_for(entered.wait(), 1)
        self.extension.close()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(chat_scheduling.tasks, {})

    async def test_custom_stage_guard_can_reject_before_executor_runs(self):
        execute = AsyncMock(return_value="should not run")
        self.extension.on("chat_request", lambda ctx: setattr(ctx, "executor", execute))
        self.extension.on("stage_guard", lambda ctx, details: details["stage"] != "turn")
        response = await self.service.process(self.request, self.runtime)
        self.assertEqual(response.text, "")
        execute.assert_not_awaited()

    async def test_authorization_revocation_discards_generated_result(self):
        from tests.migration.test_router import event

        self.runtime.event = event()
        allowed = [True]
        self.runtime.authorizer = SimpleNamespace(check=AsyncMock(side_effect=lambda *args: allowed[0]))

        async def revoke(**kwargs):
            allowed[0] = False
            return LLMResponse(content="must not send")

        self.provider.chat.side_effect = revoke
        result = await self.ctx.request_llm(LLMRequest(provider_id="test"))
        self.assertEqual(result.text, "")
        self.runtime.sender.send.assert_not_awaited()

    async def test_auxiliary_model_tasks_do_not_receive_reply_hooks(self):
        from fxbot.chat.providers.routing import TaskProvider

        before = AsyncMock()
        self.extension.on("llm_request", before)
        token = current_chat.set(self.ctx)
        try:
            async with TaskProvider(["test"], task="utils") as provider:
                await provider.chat(messages=[])
        finally:
            current_chat.reset(token)
        before.assert_not_awaited()

    async def test_nested_independent_context_keeps_its_own_hooks(self):
        seen = []
        second = self.ctx.new_turn()

        async def before(ctx):
            seen.append(ctx.request.turn_id)
            if ctx is self.ctx:
                await hooks.dispatch("chat_request", second)

        self.extension.on("chat_request", before)
        await hooks.dispatch("chat_request", self.ctx)
        self.assertEqual(seen, [self.ctx.request.turn_id, second.request.turn_id])

    async def test_clearing_owned_flow_does_not_cancel_clear_halfway(self):
        from fxbot.core.application import Application

        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        self.runtime.application = app
        complete = []

        async def flow():
            await self.ctx.clear_history()
            complete.append(True)

        with (
            patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()),
            patch("fxbot.chat.focus.focus_mode_manager.clear_context", AsyncMock()),
        ):
            await self.extension.claim_session(self.ctx, AsyncMock())
            await chat_scheduling.spawn(self.ctx, flow)
        self.assertEqual(complete, [True])
        self.assertNotEqual(self.store.epoch(self.ctx.session_id), self.request.accepted_epoch)

    async def test_native_continuation_uses_mutable_request_and_response_hooks(self):
        from fxbot.chat.providers.routing import TaskProvider

        self.provider.chat_continuation = AsyncMock(return_value=LLMResponse(content="native"))
        initial = await self.ctx.llm_generate(LLMRequest(provider_id="test", temperature=0.2, max_tokens=50))
        operations = []

        def before(ctx, request):
            operations.append(request.operation)
            request.messages.append({"role": "user", "content": "hook continuation"})
            request.temperature = 0.4

        def after(ctx, request, response):
            response.content = "filtered"

        self.extension.on("llm_request", before)
        self.extension.on("llm_response", after)
        token = current_chat.set(self.ctx)
        try:
            async with TaskProvider(["test"], task="reply") as provider:
                result = await provider.chat_continuation(messages=[], previous=initial)
        finally:
            current_chat.reset(token)
        self.assertEqual(operations, ["continuation"])
        self.assertEqual(result.content, "filtered")
        self.assertEqual(self.provider.chat_continuation.await_args.kwargs["temperature"], 0.4)
        self.assertEqual(result.request.model, "model")
        self.assertEqual(result.request.messages[-1]["content"], "hook continuation")

    async def test_continuation_model_change_regenerates_without_native_state(self):
        from fxbot.chat.providers.routing import TaskProvider

        self.provider.chat_continuation = AsyncMock()
        initial = await self.ctx.llm_generate(LLMRequest(provider_id="test"))

        def before(ctx, request):
            if request.operation == "continuation":
                request.model = "different-model"

        self.extension.on("llm_request", before)
        token = current_chat.set(self.ctx)
        try:
            async with TaskProvider(["test"], task="reply") as provider:
                result = await provider.chat_continuation(messages=[], previous=initial)
        finally:
            current_chat.reset(token)
        self.provider.chat_continuation.assert_not_awaited()
        self.assertEqual(self.provider.chat.await_args.kwargs["model"], "different-model")
        self.assertNotIn("previous", self.provider.chat.await_args.kwargs)
        self.assertEqual(result.request.model, "different-model")

    async def test_continuation_and_regeneration_keep_actual_initial_provider(self):
        from fxbot.chat.providers.routing import TaskProvider
        from fxbot.chat.replyer.generator import _continue_reasoning

        second = SimpleNamespace(
            provider_id="second",
            get_model=lambda: "second-model",
            chat=AsyncMock(side_effect=[LLMResponse(reasoning_content="thinking"), LLMResponse(content="final")]),
            chat_continuation=AsyncMock(return_value=None),
        )

        @asynccontextmanager
        async def lease(name):
            yield second if name == "second" else self.provider

        def before(ctx, request):
            if request.operation == "chat":
                request.provider_id, request.model = "second", "selected-model"
                request.temperature, request.max_tokens = 0.2, 55
                request.messages = [{"role": "user", "content": "actual prompt"}]

        self.extension.on("llm_request", before)
        token = current_chat.set(self.ctx)
        try:
            with patch.object(provider_manager, "lease", lease):
                async with TaskProvider(["test"], task="reply") as provider:
                    initial = await provider.chat(messages=[{"role": "user", "content": "original"}])
                    result = await _continue_reasoning(provider, [], initial)
        finally:
            current_chat.reset(token)
        self.provider.chat.assert_not_awaited()
        second.chat_continuation.assert_awaited_once()
        self.assertEqual(second.chat_continuation.await_args.kwargs["model"], "selected-model")
        self.assertEqual(second.chat.await_args.kwargs["messages"][0]["content"], "actual prompt")
        self.assertEqual(second.chat.await_args.kwargs["temperature"], 0.2)
        self.assertEqual(second.chat.await_args.kwargs["max_tokens"], 55)
        self.assertEqual(result.content, "final")

    async def test_continuation_use_hooks_false_and_auxiliary_task_stay_isolated(self):
        from fxbot.chat.providers.routing import TaskProvider

        self.provider.chat_continuation = AsyncMock(return_value=LLMResponse(content="native"))
        initial = await self.ctx.llm_generate(LLMRequest(provider_id="test", use_hooks=False))
        before, after = AsyncMock(), AsyncMock()
        self.extension.on("llm_request", before)
        self.extension.on("llm_response", after)
        token = current_chat.set(self.ctx)
        try:
            async with TaskProvider(["test"], task="reply") as provider:
                await provider.chat_continuation(messages=[], previous=initial)
            initial.request.use_hooks = True
            async with TaskProvider(["test"], task="utils") as provider:
                await provider.chat_continuation(messages=[], previous=initial)
        finally:
            current_chat.reset(token)
        before.assert_not_awaited()
        after.assert_not_awaited()

    async def test_continuation_revocation_does_not_regenerate(self):
        from fxbot.chat.providers.routing import TaskProvider
        from fxbot.chat.replyer.generator import _continue_reasoning

        initial = await self.ctx.llm_generate(LLMRequest(provider_id="test"))
        self.provider.chat_continuation = AsyncMock()

        def before(ctx, request):
            if request.operation == "continuation":
                self.store.clear(ctx.session_id)

        self.extension.on("llm_request", before)
        token = current_chat.set(self.ctx)
        try:
            async with TaskProvider(["test"], task="reply") as provider:
                with self.assertRaises(ChatExpired):
                    await _continue_reasoning(provider, [], initial)
        finally:
            current_chat.reset(token)
        self.provider.chat_continuation.assert_not_awaited()
        self.assertEqual(self.provider.chat.await_count, 1)

    async def test_deliver_waits_for_owner_transition_to_finish(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        scheduling = ChatScheduling()
        self.addAsyncCleanup(scheduling.stop)

        async def detach(*args):
            entered.set()
            await release.wait()

        receive = AsyncMock()
        with patch("fxbot.chat.focus.focus_mode_manager.detach", detach):
            claim = asyncio.create_task(scheduling.claim("owner", self.ctx, receive))
            await asyncio.wait_for(entered.wait(), 1)
            delivery = asyncio.create_task(scheduling.deliver(self.ctx.new_turn()))
            await asyncio.sleep(0)
            receive.assert_not_awaited()
            release.set()
            await asyncio.wait_for(asyncio.gather(claim, delivery), 1)
        receive.assert_awaited_once()

    async def test_concurrent_replacements_do_not_deliver_to_superseded_owner(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        scheduling = ChatScheduling()
        self.addAsyncCleanup(scheduling.stop)

        async def detach(*args):
            entered.set()
            await release.wait()

        first, second, third = AsyncMock(), AsyncMock(), AsyncMock()
        second_context, third_context = self.ctx.new_turn(), self.ctx.new_turn()
        with patch("fxbot.chat.focus.focus_mode_manager.detach", detach):
            one = asyncio.create_task(scheduling.claim("first", self.ctx, first))
            await asyncio.wait_for(entered.wait(), 1)
            two = asyncio.create_task(scheduling.claim("second", second_context, second, replace=True))
            await asyncio.sleep(0)
            three = asyncio.create_task(scheduling.claim("third", third_context, third, replace=True))
            await asyncio.sleep(0)
            delivery = asyncio.create_task(scheduling.deliver(self.ctx.new_turn()))
            release.set()
            await asyncio.wait_for(asyncio.gather(one, two, three, delivery), 1)
        self.assertEqual(scheduling.owner(self.ctx.session_id).name, "third")
        first.assert_not_awaited()
        second.assert_not_awaited()
        third.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError, "不属于"):
            scheduling.spawn(self.ctx, AsyncMock())

    async def test_cancelled_replacement_waits_for_old_cleanup_without_second_cancel(self):
        from fxbot.core.application import Application

        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        self.runtime.application = app
        scheduling = ChatScheduling()
        self.addAsyncCleanup(scheduling.stop)
        entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
        self.addCleanup(release.set)

        async def flow():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await scheduling.claim("old", self.ctx, AsyncMock())
            running = scheduling.spawn(self.ctx, flow)
            await asyncio.wait_for(entered.wait(), 1)
            replacement = asyncio.create_task(scheduling.claim("new", self.ctx.new_turn(), AsyncMock(), replace=True))
            await asyncio.wait_for(cleaning.wait(), 1)
            replacement.cancel()
            await asyncio.sleep(0)
            self.assertFalse(cleaned.is_set())
            self.assertFalse(replacement.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(replacement, 1)
            await asyncio.gather(running, return_exceptions=True)
        self.assertTrue(cleaned.is_set())
        self.assertIsNone(scheduling.owner(self.ctx.session_id))
        self.assertEqual(scheduling._transitions, {})

    async def test_continuation_provider_change_does_not_replay_native_payload(self):
        from fxbot.chat.providers.routing import TaskProvider

        second = SimpleNamespace(
            provider_id="second",
            get_model=lambda: "other",
            chat=AsyncMock(return_value=LLMResponse(content="portable")),
            chat_continuation=AsyncMock(),
        )

        @asynccontextmanager
        async def lease(name):
            yield second if name == "second" else self.provider

        initial = await self.ctx.llm_generate(
            LLMRequest(
                provider_id="test",
                messages=[
                    {
                        "role": "assistant",
                        "content": "",
                        "provider_data": {"response_id": "old-native-id"},
                    }
                ],
            )
        )
        initial.provider_data["output_items"] = [{"type": "reasoning", "id": "old-native-id"}]
        self.extension.on("llm_request", lambda ctx, request: setattr(request, "provider_id", "second"))
        token = current_chat.set(self.ctx)
        try:
            with patch.object(provider_manager, "lease", lease):
                async with TaskProvider(["test"], task="reply") as provider:
                    result = await provider.chat_continuation(messages=initial.request.messages, previous=initial)
        finally:
            current_chat.reset(token)
        second.chat_continuation.assert_not_awaited()
        self.assertNotIn("old-native-id", str(second.chat.await_args))
        self.assertEqual(result.request.provider_id, "second")

    async def test_continuation_timeout_releases_native_lease(self):
        from fxbot.chat.providers.routing import TaskProvider

        async def wait(**kwargs):
            await asyncio.Event().wait()

        self.provider.chat_continuation = AsyncMock(side_effect=wait)
        initial = await self.ctx.llm_generate(LLMRequest(provider_id="test"))
        self.extension.on("llm_request", lambda ctx, request: setattr(request, "timeout", 0.01))
        token = current_chat.set(self.ctx)
        try:
            with self.assertRaises(TimeoutError):
                async with TaskProvider(["test"], task="reply") as provider:
                    await provider.chat_continuation(messages=[], previous=initial)
        finally:
            current_chat.reset(token)
        self.assertEqual(self.leases[-1], ("close", "test"))

    async def test_continuation_clearing_model_to_default_does_not_reuse_other_model_state(self):
        from fxbot.chat.providers.routing import TaskProvider

        self.provider.chat_continuation = AsyncMock()
        initial = await self.ctx.llm_generate(LLMRequest(provider_id="test", model="non-default"))
        self.extension.on("llm_request", lambda ctx, request: setattr(request, "model", None))
        token = current_chat.set(self.ctx)
        try:
            async with TaskProvider(["test"], task="reply") as provider:
                result = await provider.chat_continuation(messages=[], previous=initial)
        finally:
            current_chat.reset(token)
        self.provider.chat_continuation.assert_not_awaited()
        self.assertEqual(result.request.model, "model")

    async def test_continuation_tool_calls_use_standard_loop_instead_of_being_discarded(self):
        from fxbot.chat.providers.routing import TaskProvider
        from fxbot.chat.replyer.generator import _continue_reasoning

        registry = self.registry()
        self.provider.chat.side_effect = [
            LLMResponse(reasoning_content="thinking"),
            LLMResponse(tool_calls=[call()]),
            LLMResponse(content="after tool"),
        ]

        def before(ctx, request):
            if request.operation == "continuation":
                request.tool_registry = registry

        self.extension.on("llm_request", before)
        token = current_chat.set(self.ctx)
        try:
            async with TaskProvider(["test"], task="reply") as provider:
                initial = await provider.chat(messages=[])
                result = await _continue_reasoning(provider, [], initial)
        finally:
            current_chat.reset(token)
        registry.get("lookup").handler.assert_awaited_once()
        self.assertEqual(result.content, "after tool")

    async def test_release_and_replacement_do_not_cancel_new_owner_task(self):
        from fxbot.core.application import Application

        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        self.runtime.application = app
        scheduling = ChatScheduling()
        self.addAsyncCleanup(scheduling.stop)
        entered, cleaning, release, new_started = (asyncio.Event() for _ in range(4))
        self.addCleanup(release.set)

        async def old_flow():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        async def new_flow():
            new_started.set()

        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await scheduling.claim("old", self.ctx, AsyncMock())
            scheduling.spawn(self.ctx, old_flow)
            await asyncio.wait_for(entered.wait(), 1)
            retiring = asyncio.create_task(scheduling.release("old", self.ctx.session_id))
            await asyncio.wait_for(cleaning.wait(), 1)
            new_context = self.ctx.new_turn()
            claiming = asyncio.create_task(scheduling.claim("new", new_context, AsyncMock(), replace=True))
            await asyncio.sleep(0)
            new_task = scheduling.spawn(new_context, new_flow, key="new")
            await asyncio.sleep(0)
            self.assertFalse(new_started.is_set())
            release.set()
            await asyncio.wait_for(asyncio.gather(retiring, claiming, new_task), 1)
        self.assertFalse(new_task.cancelled())
        self.assertTrue(new_started.is_set())
        self.assertEqual(scheduling.owner(self.ctx.session_id).name, "new")

    async def test_shutdown_cancels_pending_transition_and_rejects_new_claims(self):
        scheduling = ChatScheduling()
        entered = asyncio.Event()

        async def detach(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch("fxbot.chat.focus.focus_mode_manager.detach", detach):
            claim = asyncio.create_task(scheduling.claim("old", self.ctx, AsyncMock()))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(scheduling.stop(), 1)
        self.assertTrue(claim.cancelled())
        self.assertEqual(scheduling._transitions, {})
        with self.assertRaisesRegex(RuntimeError, "关闭"):
            await scheduling.claim("new", self.ctx, AsyncMock())
        scheduling.start()

    async def test_shutdown_joins_owned_managed_execution_not_only_spawned_jobs(self):
        scheduling = ChatScheduling()
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def execute(ctx):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        self.extension.on("chat_request", lambda ctx: setattr(ctx, "executor", execute))
        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await scheduling.claim("owner", self.ctx, AsyncMock())
        task = asyncio.create_task(self.service.process(self.request, self.runtime))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(scheduling.stop(), 1)
        self.assertTrue(task.cancelled())
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.service.running(self.ctx.session_id), ())

    async def test_claim_from_current_default_worker_does_not_cancel_itself(self):
        from fxbot.chat.behavior.turn_scheduler import ScheduledChat, normal_scheduler

        current = asyncio.current_task()
        normal_scheduler.chats[self.ctx.session_id] = ScheduledChat(self.service, self.runtime, task=current)
        self.addCleanup(normal_scheduler.chats.pop, self.ctx.session_id, None)
        scheduling = ChatScheduling()
        self.addAsyncCleanup(scheduling.stop)
        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await scheduling.claim("owner", self.ctx, AsyncMock())
        self.assertEqual(current.cancelling(), 0)
        self.assertNotIn(self.ctx.session_id, normal_scheduler.chats)

    async def test_clear_from_current_default_worker_completes_before_expiring_it(self):
        from fxbot.chat.behavior.turn_scheduler import ScheduledChat, normal_scheduler

        current = asyncio.current_task()
        normal_scheduler.chats[self.ctx.session_id] = ScheduledChat(self.service, self.runtime, task=current)
        self.addCleanup(normal_scheduler.chats.pop, self.ctx.session_id, None)
        with patch("fxbot.chat.focus.focus_mode_manager.clear_context", AsyncMock()):
            await self.ctx.clear_history()
        self.assertEqual(current.cancelling(), 0)
        self.assertNotEqual(self.store.epoch(self.ctx.session_id), self.request.accepted_epoch)


if __name__ == "__main__":
    unittest.main()
