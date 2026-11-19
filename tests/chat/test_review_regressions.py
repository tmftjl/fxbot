"""开放聊天审查回归：流式、Focus、消息重投、适配复用和学习标志。"""

from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from fxbot.chat import router, message_adapter
from fxbot.core import Message, SessionTarget, MessageSegment
from fxbot.config import SYSTEM_DEFAULTS, get_manager
from fxbot.chat.focus import group_runtime
from fxbot.chat.tools import ToolSpec, ToolRuntime, ToolRegistry
from fxbot.chat.types import ChatRequest
from fxbot.chat.context import ChatContext, ChatExpired, current_chat
from fxbot.chat.service import ChatService
from fxbot.chat.session import ChatSessionStore
from fxbot.chat.behavior import turn_scheduler
from fxbot.chat.providers import LLMRequest, LLMResponse, provider_manager
from fxbot.chat.extensions import ChatExtension
from fxbot.chat.scheduling import GateDecision, chat_scheduling
from fxbot.core.application import Application
from fxbot.chat.session_hooks import SessionPolicy
from fxbot.chat.message_receive import history
from tests.migration.test_router import event
from fxbot.chat.providers.routing import TaskProvider
from fxbot.chat.heart_flow.conversation import request_step


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chat_scheduling.start()
        self.config = deepcopy(SYSTEM_DEFAULTS)
        self.config["chat"].update(enabled=True, rest_time_no_reply=False, max_private_context_size=30)
        self.enterContext(patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(self.config)))
        self.enterContext(patch("fxbot.chat.memory.state.is_ready", return_value=False))
        self.enterContext(patch.object(router.emoji_module, "collect_in_background"))
        self.store = ChatSessionStore()
        self.service = ChatService(self.store)
        self.request = ChatRequest("test:bot:private:user", "user", None, "hello", "turn", "d1", "private")
        self.request.accepted_epoch = 0
        self.runtime = ToolRuntime()
        self.context = ChatContext(self.service, self.request, self.runtime)
        self.extension = ChatExtension("review-regressions")
        self.addCleanup(self.extension.close)
        self.addAsyncCleanup(chat_scheduling.stop)
        self.provider = SimpleNamespace(
            provider_id="fixture", get_model=lambda: "model", chat=AsyncMock(return_value=LLMResponse(content="answer"))
        )
        self.closed = False

        async def stream(**kwargs):
            try:
                yield LLMResponse(content="part", is_stream_chunk=True)
                yield LLMResponse(content="complete")
            finally:
                self.closed = True

        self.provider.chat_stream = stream
        self.leases = []

        @asynccontextmanager
        async def lease(name):
            self.leases.append(("open", name))
            try:
                yield self.provider
            finally:
                self.leases.append(("close", name))

        self.enterContext(patch.object(provider_manager, "lease", lease))

    async def collect(self, request=None, task="planner"):
        token = current_chat.set(self.context)
        try:
            async with TaskProvider(["fixture"], task=task) as provider:
                return [item async for item in provider.chat_stream(_request=request)]
        finally:
            current_chat.reset(token)

    async def test_real_streamed_planner_path_dispatches_both_model_hooks_once(self):
        seen = []

        async def stream(**kwargs):
            self.assertEqual(kwargs["messages"][0]["content"], "plugin system")
            yield LLMResponse(content="partial", is_stream_chunk=True)
            yield LLMResponse(content="original")

        def before(ctx, request):
            seen.append("before")
            request.messages.insert(0, {"role": "system", "content": "plugin system"})

        def after(ctx, request, response):
            seen.append("after")
            response.content = "filtered"

        self.provider.chat_stream = stream
        self.extension.on("llm_request", before)
        self.extension.on("llm_response", after)
        token = current_chat.set(self.context)
        try:
            async with TaskProvider(["fixture"], task="planner") as provider:
                result = await request_step(provider, [], [], stream=True)
        finally:
            current_chat.reset(token)
        self.assertEqual(result.content, "filtered")
        self.assertEqual(seen, ["before", "after"])
        self.assertEqual(result.request.messages[0]["content"], "plugin system")

    async def test_stream_without_terminal_frame_synthesizes_one_hooked_final(self):
        async def stream(**kwargs):
            yield LLMResponse(content="one", is_stream_chunk=True)
            yield LLMResponse(content="two", is_stream_chunk=True)

        after = AsyncMock()
        self.provider.chat_stream = stream
        self.extension.on("llm_response", after)
        result = await self.collect()
        self.assertEqual(result[-1].content, "onetwo")
        self.assertFalse(result[-1].is_stream_chunk)
        after.assert_awaited_once()

    async def test_stream_redirect_runs_hooks_once_and_releases_both_leases(self):
        before, after = [], []

        def redirect(ctx, request):
            before.append(True)
            request.provider_id = "second"

        self.extension.on("llm_request", redirect)
        self.extension.on("llm_response", lambda *args: after.append(True))
        await self.collect()
        self.assertEqual(before, [True])
        self.assertEqual(after, [True])
        self.assertEqual(len(self.leases), 4)
        self.assertTrue(self.closed)

    async def test_stream_timeout_and_early_close_close_native_generator(self):
        async def stream(**kwargs):
            try:
                yield LLMResponse(content="part", is_stream_chunk=True)
                await asyncio.Event().wait()
            finally:
                self.closed = True

        self.provider.chat_stream = stream
        after = AsyncMock()
        self.extension.on("llm_response", after)
        # 先启动原生生成器，测试已打开流的超时清理，而不是依赖首次调度是否赶在 10ms 内。
        opened = stream()
        await anext(opened)
        self.addAsyncCleanup(opened.aclose)
        self.provider.chat_stream = lambda **kwargs: opened
        with self.assertRaises(TimeoutError):
            await self.collect(LLMRequest(timeout=0.2))
        self.assertTrue(self.closed)
        self.closed = False
        self.provider.chat_stream = stream
        token = current_chat.set(self.context)
        try:
            async with TaskProvider(["fixture"], task="planner") as provider:
                response = provider.chat_stream()
                await anext(response)
                await response.aclose()
        finally:
            current_chat.reset(token)
        self.assertTrue(self.closed)
        after.assert_not_awaited()

    async def test_stream_rechecks_epoch_after_chunk(self):
        async def stream(**kwargs):
            try:
                yield LLMResponse(content="part", is_stream_chunk=True)
                self.store.clear(self.request.session_id)
                yield LLMResponse(content="stale")
            finally:
                self.closed = True

        self.provider.chat_stream = stream
        after = AsyncMock()
        self.extension.on("llm_response", after)
        with self.assertRaises(ChatExpired):
            await self.collect()
        self.assertTrue(self.closed)
        after.assert_not_awaited()

    async def test_stream_budget_is_total_not_reset_for_each_chunk(self):
        async def stream(**kwargs):
            try:
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    yield LLMResponse(content="part", is_stream_chunk=True)
            finally:
                self.closed = True

        opened = stream()
        await anext(opened)
        self.addAsyncCleanup(opened.aclose)
        self.provider.chat_stream = lambda **kwargs: opened
        with self.assertRaises(TimeoutError):
            await self.collect(LLMRequest(timeout=0.035))
        self.assertTrue(self.closed)

    async def test_stream_cancellation_closes_native_stream_without_response_hook(self):
        entered = asyncio.Event()

        async def stream(**kwargs):
            try:
                entered.set()
                yield LLMResponse(content="part", is_stream_chunk=True)
                await asyncio.Event().wait()
            finally:
                self.closed = True

        self.provider.chat_stream = stream
        after = AsyncMock()
        self.extension.on("llm_response", after)
        task = asyncio.create_task(self.collect())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.closed)
        after.assert_not_awaited()

    async def test_custom_group_validity_does_not_override_epoch_clear(self):
        context = ChatContext(self.service, self.request, self.runtime, is_valid=lambda: True)
        self.store.clear(self.request.session_id)
        with self.assertRaises(ChatExpired):
            context.check_current()

    async def test_reply_registry_is_not_installed_on_shared_runtime(self):
        registry = ToolRegistry()
        registry.register(ToolSpec("local_only", "local", {}, AsyncMock()))

        def before(ctx, request):
            if request.phase == "reply":
                request.tool_registry = registry

        self.extension.on("llm_request", before)
        response = await self.context.llm_generate(LLMRequest(provider_id="fixture"))
        self.assertIs(response.request.tool_registry, registry)
        self.assertIsNone(self.runtime.registry)
        await self.context.llm_generate(LLMRequest(provider_id="fixture", phase="planner"))
        self.assertIsNone(self.provider.chat.await_args.kwargs["tools"])

    async def test_owner_replay_delivers_once_and_records_session_activity(self):
        app = Application()
        bot = SimpleNamespace(platform="test", bot_id="bot", send=AsyncMock())
        receive = AsyncMock()
        with (
            patch.object(group_runtime, "chat_service", self.service),
            patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()),
        ):
            await self.extension.claim_session(self.context, receive)
        router.install(app, SimpleNamespace(check=AsyncMock(return_value=True)))
        with patch.object(router, "chat_service", self.service):
            await app.router.dispatch(bot, event("one"))
            await app.router.dispatch(bot, event("one"))
        receive.assert_awaited_once()
        self.assertEqual(self.store.activity(self.request.session_id), 1)

    async def test_adaptation_is_shared_but_working_requests_are_isolated(self):
        bot = SimpleNamespace(platform="test", bot_id="bot")
        incoming = event()
        with (
            patch.object(message_adapter, "_reply_info", AsyncMock(return_value=None)),
            patch.object(message_adapter, "_image_entries", AsyncMock(return_value=[])) as images,
            patch.object(message_adapter, "transcribe_event", AsyncMock(return_value="voice")) as voice,
            patch.object(message_adapter, "collect_mentions", AsyncMock(return_value=[])),
            patch.object(message_adapter, "load_images", AsyncMock(return_value=[])),
        ):
            first, second = await asyncio.gather(
                message_adapter.adapt_message_event(bot, incoming, trigger_kind="private"),
                message_adapter.adapt_message_event(bot, incoming, trigger_kind="proactive"),
            )
            first.metadata["raw_images"].append("modified")
            third = await message_adapter.adapt_message_event(bot, incoming, trigger_kind="mention")
        images.assert_awaited_once()
        voice.assert_awaited_once()
        self.assertEqual(third.metadata["raw_images"], [])
        self.assertEqual(second.trigger_kind, "proactive")
        self.assertNotEqual(first.turn_id, second.turn_id)
        self.assertIs(third.metadata["event"], incoming)

    async def test_observe_collect_and_scheduler_do_not_repeat_media_adaptation(self):
        app = Application()
        bot = SimpleNamespace(platform="test", bot_id="bot", send=AsyncMock())
        incoming = replace(
            event(),
            target=SessionTarget("test", "bot", "group", "group"),
            message=Message([MessageSegment("voice"), MessageSegment("image")]),
        )
        self.extension.on("message", lambda ctx: None)
        router.install(app, SimpleNamespace(check=AsyncMock(return_value=True)))
        with (
            patch.object(router, "chat_service", self.service),
            patch.object(router, "direct_reply_mode", return_value=False),
            patch.object(router, "_context_rule", AsyncMock(return_value=True)),
            patch.object(router, "_ai_fallback_rule", AsyncMock(return_value=False)),
            patch.object(router.focus_mode_manager, "enabled", return_value=False),
            patch.object(router.focus_mode_manager, "attached", return_value=False),
            patch.object(router.normal_scheduler, "submit", AsyncMock()) as submit,
            patch.object(self.service, "record_group_message", AsyncMock()) as record,
            patch.object(router, "transcribe_event", AsyncMock()) as extra_voice,
            patch.object(message_adapter, "_reply_info", AsyncMock(return_value=None)),
            patch.object(message_adapter, "collect_mentions", AsyncMock(return_value=[])),
            patch.object(message_adapter, "_image_entries", AsyncMock(return_value=[("1", "raw", False)])) as images,
            patch.object(message_adapter, "transcribe_event", AsyncMock(return_value="voice")) as voice,
            patch.object(
                message_adapter, "load_images", AsyncMock(return_value=["data:image/png;base64,AA=="])
            ) as load,
        ):
            result = await app.router.dispatch(bot, incoming)
        self.assertFalse(result.failed)
        images.assert_awaited_once()
        voice.assert_awaited_once()
        load.assert_awaited_once()
        extra_voice.assert_not_awaited()
        record.assert_awaited_once()
        submit.assert_awaited_once()

    async def test_context_only_focus_request_preserves_learning_flag(self):
        bot = SimpleNamespace(platform="test", bot_id="bot")
        incoming = replace(event("text"), target=SessionTarget("test", "bot", "group", "group"))
        incoming.metadata["_chat_learn"] = False
        adapted = replace(self.request, session_id=incoming.target.key(), group_id="group")
        with (
            patch.object(router, "chat_service", self.service),
            patch.object(router, "direct_reply_mode", return_value=False),
            patch.object(router, "collect_mentions", AsyncMock(return_value=[])),
            patch.object(router, "memory_image_sources", AsyncMock(return_value=[])),
            patch.object(router, "read_quote_snapshot", AsyncMock(return_value=None)),
            patch.object(router, "adapt_message_event", AsyncMock(return_value=adapted)),
            patch.object(self.service, "record_group_message", AsyncMock()),
            patch.object(router.focus_mode_manager, "enabled", return_value=True),
            patch.object(router.focus_mode_manager, "is_enabled_for_request", return_value=True),
            patch.object(router, "_focus_or_detach", AsyncMock()) as focus,
        ):
            await router._handle_context(bot, incoming, Application(), SimpleNamespace())
        request = focus.await_args.args[2]
        self.assertFalse(request.metadata["learn"])
        self.assertFalse(ChatContext(self.service, request).learn)

    async def test_focus_planner_new_member_message_does_not_invalidate_paid_response(self):
        group = group_runtime.FocusGroupRuntime("focus-test")
        incoming = event()
        self.request.trigger_seq = self.store.next_seq(self.request.session_id, self.request.user_id)
        group.members[self.request.session_id] = group_runtime.FocusMember(
            self.request.session_id,
            SimpleNamespace(),
            incoming,
            incoming.target,
            self.request,
            False,
            Application(),
            SimpleNamespace(check=AsyncMock(return_value=True)),
        )

        async def advance(**kwargs):
            self.store.next_seq(self.request.session_id, self.request.user_id)
            return LLMResponse(content="paid response")

        self.provider.chat.side_effect = advance
        with (
            patch.object(group_runtime, "chat_service", self.service),
            patch.object(group_runtime, "fallback_provider", lambda *args: TaskProvider(["fixture"], task="planner")),
        ):
            result, _, _ = await group._chat_guarded([], [])
        self.assertEqual(result.content, "paid response")
        self.assertEqual(group._failures, 0)

    async def test_focus_expiration_does_not_enter_failure_backoff(self):
        group = group_runtime.FocusGroupRuntime("focus-expiry")
        group.members["member"] = SimpleNamespace()
        attempts = []

        async def step():
            attempts.append(True)
            if len(attempts) == 1:
                raise ChatExpired("cleared")
            group._closed = True

        with (
            patch.object(group, "_idle_or_turn", side_effect=step),
            patch.object(group, "checkpoint", AsyncMock()),
            patch.object(group_runtime.asyncio, "sleep", AsyncMock()) as backoff,
        ):
            await group.run()
        backoff.assert_not_awaited()

    async def test_focus_planner_still_rejects_cleared_group_epochs(self):
        group = group_runtime.FocusGroupRuntime("focus-clear")
        incoming = event()
        group.members[self.request.session_id] = group_runtime.FocusMember(
            self.request.session_id,
            SimpleNamespace(),
            incoming,
            incoming.target,
            self.request,
            False,
            Application(),
            SimpleNamespace(check=AsyncMock(return_value=True)),
        )

        async def clear(**kwargs):
            self.store.clear(self.request.session_id)
            return LLMResponse(content="stale")

        self.provider.chat.side_effect = clear
        with (
            patch.object(group_runtime, "chat_service", self.service),
            patch.object(group_runtime, "fallback_provider", lambda *args: TaskProvider(["fixture"], task="planner")),
        ):
            with self.assertRaises(ChatExpired):
                await group._chat_guarded([], [])

    async def test_batch_forcing_does_not_let_followup_cancel_prior_mention(self):
        scheduler = turn_scheduler.NormalChatScheduler()
        self.addAsyncCleanup(scheduler.stop)
        first = replace(self.request, group_id="g", trigger_kind="mention", dedupe_key="first")
        followup = replace(
            first, trigger_kind="proactive", dedupe_key="last", metadata={"_chat_gate": GateDecision(force=False)}
        )
        service = SimpleNamespace(is_current=lambda *args: True, process_and_send=AsyncMock())
        state = turn_scheduler.ScheduledChat(service, self.runtime, pending=[first, followup])
        state.received_at = {"first": 10**12, "last": 10**12}
        scheduler.chats[first.session_id] = state
        with patch.object(turn_scheduler, "talk_value_for", return_value=0):
            await scheduler._run(first.session_id, state)
        service.process_and_send.assert_awaited_once()

    async def test_queue_and_batch_persistence_respect_disabled_learning(self):
        active = replace(self.request, metadata={"planner_loop": True})
        self.service._active_requests[self.request.session_id] = active
        followup = replace(self.request, turn_id="next", metadata={"learn": False})
        with patch.object(self.service, "record_group_message", AsyncMock()) as record:
            await self.service.queue_message(followup, self.runtime)
        self.assertFalse(record.await_args.kwargs["learn"])
        with (
            patch.object(history.chat_repository, "append_message", AsyncMock(return_value=1)) as append,
            patch.object(history, "_prepare_image_memory", AsyncMock()),
            patch.object(history, "_enqueue_image_memory", AsyncMock()),
            patch.object(history, "_track_message", AsyncMock()) as learn,
        ):
            await history.persist_batch([followup])
        append.assert_awaited_once()
        learn.assert_not_awaited()

    async def test_direct_reply_keeps_hook_parts_metadata_and_response_identity(self):
        self.extension.on("session_policy", lambda _: SessionPolicy(direct_reply=True, allow_typing=False))
        received = []

        def reply(ctx, response):
            received.append(response)
            response.metadata.update(reply_parts=["one", "two"], custom_note={"keep": True})

        self.extension.on("reply", reply)
        self.runtime.sender = SimpleNamespace(send=AsyncMock(return_value={"message_id": "sent"}))
        with patch.object(self.service, "_persist_turn_end", AsyncMock(return_value=None)) as persist:
            response = await self.service.process(self.request, self.runtime)
        self.assertIs(response, received[0])
        self.assertEqual(response.text, "onetwo")
        self.assertEqual(response.raw.content, "onetwo")
        self.assertEqual(response.metadata["custom_note"], {"keep": True})
        self.assertEqual(persist.await_args.args[-2], ["one", "two"])
        self.assertEqual(persist.await_args.args[-1], "answer")
        await self.service._send_generated(self.request, response, self.runtime)
        self.assertEqual([call.args[0] for call in self.runtime.sender.send.await_args_list], ["one", "two"])
        self.assertEqual([item["content"] for item in self.store.get(self.request.session_id)][-2:], ["one", "two"])

    async def test_direct_reply_text_changes_override_stale_parts_including_empty_reply(self):
        self.extension.on("session_policy", lambda _: SessionPolicy(direct_reply=True))
        for text in ("changed", ""):
            with self.subTest(text=text):
                self.provider.chat.return_value = LLMResponse(content="original")

                def reply(ctx, response):
                    response.text = text
                    response.metadata.update(reply_parts=["stale"], custom_note="keep")

                self.extension.on("reply", reply)
                response = await self.service.process(self.request, self.runtime)
                self.assertEqual(response.text, text)
                self.assertEqual(response.metadata["reply_parts"], [text] if text else [])
                self.assertEqual(response.metadata["custom_note"], "keep")

    async def test_normal_reply_keeps_hook_metadata_and_parts(self):
        from fxbot.chat.behavior.response import ProcessedResponseSegment

        self.extension.on(
            "reply", lambda ctx, response: response.metadata.update(reply_parts=["one", "two"], custom_note="keep")
        )
        with (
            patch("fxbot.chat.service._memory_block", AsyncMock(return_value="")),
            patch("fxbot.chat.service._learning_reference", AsyncMock(return_value="")),
            patch(
                "fxbot.chat.service.generator.shape_reply",
                AsyncMock(
                    return_value=(
                        LLMResponse(content="answer"),
                        [ProcessedResponseSegment("answer")],
                        "answer",
                    )
                ),
            ),
            patch.object(self.service, "_persist_turn_end", AsyncMock(return_value=None)) as persist,
        ):
            async with self.store.turn_lock(self.request.session_id):
                response = await self.service._process_locked(self.request, self.runtime, focus_instruction="reply")
        self.assertEqual(response.text, "onetwo")
        self.assertEqual(response.metadata["custom_note"], "keep")
        self.assertEqual(persist.await_args.args[-2], ["one", "two"])

    async def test_closing_extension_in_own_hook_preserves_current_turn_but_cancels_siblings(self):
        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        self.runtime.application = app
        self.runtime.sender = SimpleNamespace(send=AsyncMock(return_value={"message_id": "sent"}))
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def sibling():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await self.extension.claim_session(self.context, AsyncMock())
        task = chat_scheduling.spawn(self.context, sibling, key="sibling")
        await asyncio.wait_for(entered.wait(), 1)

        async def controller(ctx):
            self.extension.close()
            await asyncio.sleep(0)
            ctx.set_result("completed")

        self.extension.on("chat_request", controller)
        self.assertTrue(await self.service.process_and_send(self.request, self.runtime))
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertTrue(cleaned.is_set())
        self.assertIsNone(chat_scheduling.owner(self.request.session_id))
        self.runtime.sender.send.assert_awaited_once_with("completed")

    async def test_owned_flow_can_close_its_extension_and_return_normally(self):
        app = Application()
        await app.start()
        self.addAsyncCleanup(app.stop)
        self.runtime.application = app
        finished = []

        async def flow():
            self.extension.close()
            await asyncio.sleep(0)
            finished.append(True)

        with patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()):
            await self.extension.claim_session(self.context, AsyncMock())
        await asyncio.wait_for(chat_scheduling.spawn(self.context, flow), 1)
        self.assertEqual(finished, [True])
        self.assertEqual(chat_scheduling.tasks, {})


if __name__ == "__main__":
    unittest.main()
