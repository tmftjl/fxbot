"""网页渠道复用门控前观察、调度接管及流式通道取消，不模拟平台身份。"""

from __future__ import annotations

import json
import asyncio
import unittest
from copy import deepcopy
from uuid import uuid4
from unittest.mock import AsyncMock, patch

from fxbot.config import SYSTEM_DEFAULTS, get_manager
from fxbot.chat.service import ChatService
from fxbot.chat.session import ChatSessionStore
from fxbot.chat.extensions import ChatExtension
from fxbot.chat.scheduling import chat_scheduling
from fxbot.core.application import Application
from fxbot.console.chat_channel import ChannelRejected, ConsoleChatChannel


class WebExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        chat_scheduling.start()
        self.config = deepcopy(SYSTEM_DEFAULTS)
        self.config["chat"].update(enabled=True, rest_time_no_reply=False, max_private_context_size=30)
        self.enterContext(patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(self.config)))
        self.enterContext(patch("fxbot.chat.memory.state.is_ready", return_value=False))
        self.enterContext(patch("fxbot.chat.focus.focus_mode_manager.detach", AsyncMock()))
        self.service = ChatService(ChatSessionStore())
        self.channel = ConsoleChatChannel(self.service)
        self.app = Application()
        await self.app.start()
        self.channel.start(self.app)
        self.addAsyncCleanup(self.app.stop)
        self.addAsyncCleanup(chat_scheduling.stop)
        self.addAsyncCleanup(self.channel.stop)
        self.extension = ChatExtension("test-web-open")
        self.addCleanup(self.extension.close)
        self.conversation = uuid4()

    async def begin(self):
        return await self.channel.begin(
            self.conversation,
            text="hello",
            message_id=uuid4(),
            nickname="tester",
            persona="",
            provider="",
        )

    async def collect(self, turn):
        return [json.loads(line) async for line in self.channel.events(turn)]

    async def test_message_and_gate_run_before_history_and_default_execution(self):
        order = []

        def message(chat):
            self.assertEqual(chat.phase, "message")
            self.assertIsNone(chat.runtime.bot)
            self.assertIsNone(chat.runtime.event)
            self.assertIs(chat.runtime.application, self.app)
            self.assertEqual(chat.history(), [])
            order.append("message")

        def gate(chat, decision):
            order.append("gate")

        async def default(request, runtime):
            order.append("default")
            return False

        self.extension.on("message", message)
        self.extension.on("gate", gate)
        with patch.object(self.service, "process_and_send", side_effect=default):
            turn = await self.begin()
            events = await self.collect(turn)
        self.assertEqual(order, ["message", "gate", "default"])
        self.assertEqual(events[0]["type"], "accepted")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(len(self.service.session_store.get(turn.request.session_id)), 1)

    async def test_gate_denial_keeps_user_history_but_does_not_call_default(self):
        self.extension.on("gate", lambda chat, decision: setattr(decision, "allow", False))
        with patch.object(self.service, "process_and_send", AsyncMock()) as default:
            turn = await self.begin()
            await self.collect(turn)
        default.assert_not_awaited()
        self.assertEqual(len(self.service.session_store.get(turn.request.session_id)), 1)

    async def test_owner_can_be_selected_and_reply_without_default_planner(self):
        received = []

        async def receive(chat):
            received.append(chat.session_id)
            await chat.submit("owned answer")

        async def select(chat):
            await self.extension.claim_session(chat, receive)

        self.extension.on("message", select)
        with patch.object(self.service, "process_and_send", AsyncMock()) as default:
            turn = await self.begin()
            events = await self.collect(turn)
        default.assert_not_awaited()
        self.assertEqual(received, [turn.request.session_id])
        self.assertEqual([item["content"] for item in events if item["type"] == "message"], ["owned answer"])
        history = self.service.session_store.get(turn.request.session_id)
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])

    async def test_owner_may_override_rest_policy_but_not_global_disable(self):
        async def select(chat):
            await self.extension.claim_session(chat, lambda context: context.submit("rest override"))

        self.extension.on("message", select)
        established = await self.begin()
        await self.collect(established)
        self.config["chat"]["rest_time_no_reply"] = True
        with patch("fxbot.chat.work_schedule.schedule_status", return_value={"allowed": False}):
            turn = await self.begin()
            events = await self.collect(turn)
        self.assertEqual([item["content"] for item in events if item["type"] == "message"], ["rest override"])
        self.config["chat"]["enabled"] = False
        with self.assertRaisesRegex(ChannelRejected, "disabled"):
            await self.begin()

    async def test_unrelated_global_hook_does_not_admit_disabled_web_session(self):
        message = AsyncMock()
        self.extension.on("message", message)
        self.config["chat"]["rest_time_no_reply"] = True
        with patch("fxbot.chat.work_schedule.schedule_status", return_value={"allowed": False}):
            with self.assertRaisesRegex(ChannelRejected, "disabled"):
                await self.begin()
        message.assert_not_awaited()
        self.assertFalse(self.channel.active)

    async def test_ingress_rejection_does_not_dispatch_message_hooks(self):
        message = AsyncMock()
        self.extension.on("message", message)
        self.extension.on("ingress_guard", lambda *args: False)
        with self.assertRaisesRegex(ChannelRejected, "ingress"):
            await self.begin()
        message.assert_not_awaited()

    async def test_explicit_stop_after_submit_does_not_duplicate_user_or_default_reply(self):
        async def message(chat):
            await chat.submit("handled")
            chat.stop_event()

        self.extension.on("message", message)
        with patch.object(self.service, "process_and_send", AsyncMock()) as default:
            turn = await self.begin()
            events = await self.collect(turn)
        default.assert_not_awaited()
        self.assertEqual(len([item for item in events if item["type"] == "message"]), 1)
        self.assertEqual(len(self.service.session_store.get(turn.request.session_id)), 2)

    async def test_message_failure_does_not_fall_back(self):
        self.extension.on("message", AsyncMock(side_effect=ValueError("broken")))
        with patch.object(self.service, "process_and_send", AsyncMock()) as default:
            turn = await self.begin()
            events = await self.collect(turn)
        default.assert_not_awaited()
        self.assertTrue(any(item["type"] == "error" for item in events))
        self.assertEqual(events[-1]["type"], "done")

    async def test_stale_turn_after_hook_cannot_record_or_start_default(self):
        self.extension.on("message", lambda chat: chat.sessions.clear(chat.session_id))
        with patch.object(self.service, "process_and_send", AsyncMock()) as default:
            turn = await self.begin()
            await self.collect(turn)
        default.assert_not_awaited()
        self.assertEqual(self.service.session_store.get(turn.request.session_id), [])

    async def test_stream_release_joins_background_flow_and_is_idempotent(self):
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

        async def receive(chat):
            chat.scheduling.spawn(chat, flow)
            await entered.wait()

        self.extension.on("message", lambda chat: self.extension.claim_session(chat, receive))
        turn = await self.begin()
        await asyncio.wait_for(turn.task, 1)
        first = asyncio.create_task(self.channel.release(turn))
        await asyncio.wait_for(cleaning.wait(), 1)
        second = asyncio.create_task(self.channel.release(turn))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(cleaned.is_set())
        release.set()
        await asyncio.wait_for(second, 1)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(chat_scheduling.tasks, {})

    async def test_old_stream_release_does_not_cancel_new_sender_flow(self):
        old = await self.begin()
        await asyncio.wait_for(old.task, 1)
        entered = asyncio.Event()

        async def flow():
            entered.set()
            await asyncio.Event().wait()

        async def receive(chat):
            chat.scheduling.spawn(chat, flow)
            await entered.wait()

        self.extension.on("message", lambda chat: self.extension.claim_session(chat, receive))
        newer = await self.begin()
        await asyncio.wait_for(newer.task, 1)
        running = next(iter(chat_scheduling.tasks.values()))
        await self.channel.release(old)
        self.assertFalse(running.done())
        await self.channel.release(newer)
        self.assertTrue(running.done())


if __name__ == "__main__":
    unittest.main()
