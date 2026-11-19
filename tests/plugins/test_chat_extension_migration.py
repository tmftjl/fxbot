"""运维与无限二代迁移后的模型 hook、热更新与原有守卫，不调用真实 shell 或模型。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from fxbot.chat import hooks
from fxbot.config import SYSTEM_DEFAULTS, get_manager
from fxbot.plugins import server_admin
from fxbot.chat.tools import ToolRuntime
from fxbot.chat.types import ChatRequest
from fxbot.chat.context import ChatContext
from fxbot.chat.service import ChatService
from fxbot.chat.session import ChatSessionStore
from fxbot.chat.providers import LLMRequest, LLMResponse, provider_manager
from fxbot.chat.session_hooks import _policies
from fxbot.plugins.infinite_gen2 import integration as infinite

BASE = "onebot11:100:private:200"
SERVER = "onebot11:100:server:private:200"


class PluginChatMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = ChatSessionStore()
        self.context = ChatContext(
            ChatService(self.store), ChatRequest(SERVER, "200", None, "hello", "turn", "message", "private")
        )
        self.enterContext(patch.object(infinite, "is_enabled", return_value=False))
        self.enterContext(patch.object(server_admin, "_runtime_facts", return_value="runtime facts"))
        self.enterContext(patch.object(server_admin, "_services_note", return_value="service list"))
        self.enterContext(patch.object(server_admin, "skip_confirmation", return_value=False))
        self.pending = self.enterContext(patch.object(server_admin.executor, "pending", return_value=None))

    def request(self, phase="reply"):
        return LLMRequest(
            messages=[{"role": "system", "content": "selected persona"}, {"role": "user", "content": "hello"}],
            provider_id="chosen",
            model="chosen-model",
            temperature=0.3,
            phase=phase,
        )

    async def test_server_environment_preserves_persona_parameters_and_history(self):
        request = self.request()
        original = deepcopy(request.messages)
        await hooks.dispatch("llm_request", self.context, request)
        self.assertEqual(request.messages[0], original[0])
        self.assertEqual(request.messages[-1], original[-1])
        self.assertEqual(request.messages[1]["role"], "system")
        self.assertIn("server_exec", request.messages[1]["content"])
        self.assertIn("runtime facts", request.messages[1]["content"])
        self.assertIn("service list", request.messages[1]["content"])
        self.assertEqual((request.provider_id, request.model, request.temperature), ("chosen", "chosen-model", 0.3))
        self.assertEqual(self.store.get(SERVER), [])

    async def test_server_pending_confirmation_is_refreshed_without_duplicate_sections(self):
        request = self.request()
        self.pending.side_effect = [{"command": "old-command"}, {"command": "new-command"}]
        await hooks.dispatch("llm_request", self.context, request)
        request.operation = "continuation"
        await hooks.dispatch("llm_request", self.context, request)
        contents = [item["content"] for item in request.messages]
        self.assertEqual(sum(text.startswith(server_admin._PROMPT_MARKER) for text in contents), 1)
        self.assertNotIn("old-command", "\n".join(contents))
        self.assertIn("new-command", "\n".join(contents))

    async def test_server_confirmation_mode_is_hot_updated(self):
        request = self.request()
        await hooks.dispatch("llm_request", self.context, request)
        with patch.object(server_admin, "skip_confirmation", return_value=True):
            await hooks.dispatch("llm_request", self.context, request)
        section = request.messages[1]["content"]
        self.assertIn("全部命令免确认已开启", section)
        self.assertNotIn("回复「确认执行」", section)

    async def test_server_note_does_not_change_normal_chats_or_auxiliary_requests(self):
        request = self.request()
        original = deepcopy(request.messages)
        self.context.request.session_id = BASE
        await hooks.dispatch("llm_request", self.context, request)
        self.assertEqual(request.messages, original)
        self.context.request.session_id = SERVER
        request.phase = "utils"
        await hooks.dispatch("llm_request", self.context, request)
        self.assertEqual(request.messages, original)
        self.pending.assert_not_called()

    async def test_server_guards_and_clear_observer_remain_registered(self):
        identity = hooks.ChatHookContext(SERVER, user_id="200")
        with patch.object(server_admin, "plugin_enabled", return_value=False):
            self.assertFalse(await hooks.guard("request_guard", identity))
            self.assertFalse(await hooks.guard("send_guard", identity))
            self.assertFalse(await hooks.guard("tool_guard", identity, tool_name="server_exec"))
        with patch.object(server_admin.executor, "cancel") as cancel:
            await hooks.notify("session_clear_start", identity)
            cancel.assert_called_once_with(SERVER)

    async def test_infinite_prompt_is_available_for_reply_direct_and_focus(self):
        self.context.request.session_id = BASE
        with patch.object(infinite, "is_enabled", return_value=True):
            for phase in ("reply", "planner"):
                for context_phase in ("chat", "reply", "focus_planner"):
                    with self.subTest(phase=phase, context_phase=context_phase):
                        self.context.phase = context_phase
                        request = self.request(phase)
                        await hooks.dispatch("llm_request", self.context, request)
                        self.assertEqual(request.messages[0]["content"], "selected persona")
                        self.assertEqual(request.messages[1]["content"], infinite._PROMPT_MARKER + infinite.PROMPT_TEXT)
                        self.assertEqual(request.messages[-1]["content"], "hello")

    async def test_infinite_prompt_is_idempotent_and_removed_when_disabled(self):
        self.context.request.session_id = BASE
        request = self.request()
        original = deepcopy(request.messages)
        with patch.object(infinite, "is_enabled", return_value=True):
            for operation in ("chat", "continuation", "regeneration"):
                request.operation = operation
                await hooks.dispatch("llm_request", self.context, request)
        self.assertEqual(len(request.messages), len(original) + 1)
        await hooks.dispatch("llm_request", self.context, request)
        self.assertEqual(request.messages, original)

    async def test_infinite_prompt_does_not_inject_into_auxiliary_tasks(self):
        self.context.request.session_id = BASE
        request = self.request("utils")
        original = deepcopy(request.messages)
        with patch.object(infinite, "is_enabled", return_value=True):
            await hooks.dispatch("llm_request", self.context, request)
        self.assertEqual(request.messages, original)

    def test_infinite_direct_chat_switch_remains_independent_of_prompt_switch(self):
        with patch.object(infinite, "direct_llm_enabled", return_value=True):
            self.assertTrue(infinite.direct_chat_policy(BASE).direct_reply)
        with patch.object(infinite, "direct_llm_enabled", return_value=False):
            self.assertIsNone(infinite.direct_chat_policy(BASE))

    async def test_both_plugins_coexist_without_overwriting_each_other_or_persona(self):
        request = self.request()
        with patch.object(infinite, "is_enabled", return_value=True):
            for _ in range(2):
                await hooks.dispatch("llm_request", self.context, request)
        sections = [item["content"] for item in request.messages if item["role"] == "system"]
        self.assertEqual(len(sections), 3)
        self.assertEqual(sections[0], "selected persona")
        self.assertEqual(sum(item.startswith(infinite._PROMPT_MARKER) for item in sections), 1)
        self.assertEqual(sum(item.startswith(server_admin._PROMPT_MARKER) for item in sections), 1)

    def test_reinstall_does_not_duplicate_infinite_callbacks_or_policy(self):
        infinite.install()
        infinite.install()
        self.assertEqual(
            sum(item.callback is infinite.prepare_request for item in hooks._hooks["llm_request"].values()), 1
        )
        self.assertEqual(sum(item.callback is infinite.direct_chat_policy for item in _policies.values()), 1)


class PluginChatPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = deepcopy(SYSTEM_DEFAULTS)
        config["chat"].update(enabled=True, rest_time_no_reply=False)
        self.enterContext(patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(config)))
        self.enterContext(patch("fxbot.chat.memory.state.is_ready", return_value=False))
        self.enterContext(patch.object(server_admin, "_runtime_facts", return_value="runtime facts"))
        self.enterContext(patch.object(server_admin, "_services_note", return_value="service list"))
        self.enterContext(patch.object(server_admin.executor, "pending", return_value=None))
        self.enterContext(patch.object(server_admin, "plugin_enabled", return_value=True))
        self.enterContext(patch.object(server_admin, "is_superuser", return_value=True))
        self.enterContext(patch.object(server_admin.store, "enabled", return_value=True))
        self.enterContext(patch.object(infinite, "is_enabled", return_value=True))
        self.enterContext(patch.object(infinite, "direct_llm_enabled", return_value=False))
        self.store = ChatSessionStore()
        self.service = ChatService(self.store)
        self.sender = SimpleNamespace(send=AsyncMock(return_value={"message_id": "sent"}))
        self.runtime = ToolRuntime(sender=self.sender)
        self.provider = SimpleNamespace(
            provider_id="fixture",
            get_model=lambda: "fixture-model",
            chat=AsyncMock(return_value=LLMResponse(content="final answer")),
        )

        @asynccontextmanager
        async def lease(name):
            yield self.provider

        self.enterContext(patch.object(provider_manager, "lease", lease))

    async def test_server_custom_request_applies_both_new_hooks_and_standard_history(self):
        request = ChatRequest(SERVER, "200", None, "hello", "turn", "message", "private")
        context = ChatContext(self.service, request, self.runtime)
        with patch.object(self.service, "_process", AsyncMock()) as planner:
            await context.request_llm(
                LLMRequest(
                    provider_id="fixture",
                    messages=[{"role": "system", "content": "chosen persona"}, {"role": "user", "content": "hello"}],
                )
            )
        planner.assert_not_awaited()
        messages = self.provider.chat.await_args.kwargs["messages"]
        self.assertEqual(messages[0]["content"], "chosen persona")
        self.assertEqual(len([item for item in messages if item["role"] == "system"]), 3)
        self.sender.send.assert_awaited_once_with("final answer")
        self.assertEqual([item["role"] for item in self.store.get(SERVER)], ["user", "assistant"])

    async def test_infinite_direct_mode_keeps_plain_persona_and_avoids_planner_memory(self):
        request = ChatRequest(BASE, "200", None, "hello", "turn", "message", "private")
        with (
            patch.object(infinite, "direct_llm_enabled", return_value=True),
            patch("fxbot.chat.service.get_persona_text", return_value="plain persona"),
            patch("fxbot.chat.service._memory_block", AsyncMock()) as memory,
            patch("fxbot.chat.service._learning_reference", AsyncMock()) as learning,
        ):
            await self.service.process_and_send(request, self.runtime)
        memory.assert_not_awaited()
        learning.assert_not_awaited()
        messages = self.provider.chat.await_args.kwargs["messages"]
        self.assertEqual(messages[0]["content"], "plain persona")
        self.assertEqual(messages[1]["content"], infinite._PROMPT_MARKER + infinite.PROMPT_TEXT)
        self.assertIsNone(self.provider.chat.await_args.kwargs["tools"])
        self.sender.send.assert_awaited_once_with("final answer")

    async def test_streamed_planner_keeps_both_plugins_system_sections(self):
        from fxbot.chat.context import current_chat
        from fxbot.chat.providers.routing import TaskProvider
        from fxbot.chat.heart_flow.conversation import request_step

        async def stream(**kwargs):
            sections = [item["content"] for item in kwargs["messages"] if item["role"] == "system"]
            self.assertEqual(sections[0], "chosen persona")
            self.assertTrue(any(item.startswith(server_admin._PROMPT_MARKER) for item in sections))
            self.assertTrue(any(item.startswith(infinite._PROMPT_MARKER) for item in sections))
            yield LLMResponse(content="partial", is_stream_chunk=True)
            yield LLMResponse(content="planner result")

        self.provider.chat_stream = stream
        context = ChatContext(
            self.service, ChatRequest(SERVER, "200", None, "hello", "turn", "message", "private"), self.runtime
        )
        token = current_chat.set(context)
        try:
            async with TaskProvider(["fixture"], task="planner") as provider:
                response = await request_step(
                    provider, [{"role": "system", "content": "chosen persona"}], [], stream=True
                )
        finally:
            current_chat.reset(token)
        self.assertEqual(response.content, "planner result")
        self.provider.chat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
