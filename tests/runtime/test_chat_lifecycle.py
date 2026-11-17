"""原生聊天启动、部分失败回收与真实协议到模型替身的闭环。"""

from __future__ import annotations

import json
import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch

from fastapi.testclient import TestClient

from fxbot.chat import lifecycle
from fxbot.config import SYSTEM_DEFAULTS, ConfigManager, get_manager
from fxbot.chat.memory import state
from fxbot.core.scheduler import Job, Interval, Scheduler
from fxbot.runtime.server import create_app
from fxbot.core.application import Application, ApplicationState
from fxbot.runtime.settings import Settings
from fxbot.chat.session_hooks import SessionPolicy, register_session_policy
from fxbot.chat.providers.base import ChatProvider
from tests.runtime.test_server import HEADERS
from fxbot.chat.providers.manager import provider_manager
from fxbot.chat.providers.entities import LLMResponse
from tests.migration.test_onebot11 import payload


class ChatLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """生命周期边界使用受控依赖，业务模块仍使用共享实例。"""

    async def asyncSetUp(self) -> None:
        self.events = []
        self.app = Application()
        self.scheduler = Scheduler(self.app)
        self.config = Mock(spec=ConfigManager)
        self.config.get_system.return_value = deepcopy(SYSTEM_DEFAULTS)
        self.config.subscribe.side_effect = lambda callback: lambda: self.events.append("unsubscribe")
        self.failure = None

        def opening(name):
            return Mock(side_effect=lambda: self.events.append(f"start:{name}"))

        def action(name):
            async def run():
                self.events.append(name)
                if self.failure == name:
                    raise RuntimeError(name)

            return AsyncMock(side_effect=run)

        values = {
            "provider_manager": SimpleNamespace(
                start=action("start:providers"),
                aclose=action("stop:providers"),
                has_provider=Mock(return_value=True),
            ),
            "amemorix_runtime": SimpleNamespace(start=action("start:memory"), stop=action("stop:memory")),
            "image_writeback": SimpleNamespace(start=action("start:images"), stop=action("stop:images")),
            "chat_writeback": SimpleNamespace(start=action("start:writeback"), stop=action("stop:writeback")),
            "trimmed_learning": SimpleNamespace(start=opening("learning"), stop=action("stop:learning")),
            "subagent_tasks": SimpleNamespace(open=opening("subagents"), cancel_all=action("stop:subagents")),
            "focus_mode_manager": SimpleNamespace(start=opening("focus"), stop_all=action("stop:focus")),
            "reply_scheduler": SimpleNamespace(start=opening("continuations"), stop=action("stop:continuations")),
            "normal_scheduler": SimpleNamespace(start=opening("turns"), stop=action("stop:turns")),
            "state": SimpleNamespace(prepare_memory=action("start:schema"), mark_degraded=Mock()),
            "stop_audit_observers": action("stop:audit"),
            "close_shared_async_client": action("stop:http"),
            "set_main_loop": Mock(),
        }
        self.enterContext(patch.multiple(lifecycle, **values))
        self.feature = lifecycle.ChatLifecycle(self.app, self.config, self.scheduler)
        self.app.resource(self.feature.start, self.feature.stop)

    async def test_startup_installs_jobs_and_shutdown_preserves_unrelated_jobs(self) -> None:
        self.scheduler.set(Job("outside", Interval(60), AsyncMock()))
        await self.app.start()
        self.assertEqual(len(self.scheduler.jobs), 5)
        self.config.subscribe.assert_called_once()
        await self.app.stop()
        self.assertEqual([job.name for job in self.scheduler.jobs], ["outside"])
        self.assertLess(self.events.index("stop:focus"), self.events.index("stop:memory"))
        self.assertLess(self.events.index("stop:writeback"), self.events.index("stop:memory"))
        self.assertLess(self.events.index("stop:memory"), self.events.index("stop:providers"))
        self.assertLess(self.events.index("stop:providers"), self.events.index("stop:http"))
        self.assertIn("unsubscribe", self.events)

    async def test_partial_startup_releases_registered_resources(self) -> None:
        self.failure = "start:memory"
        with self.assertRaisesRegex(RuntimeError, "start:memory"):
            await self.app.start()
        self.assertEqual(self.app.state, ApplicationState.STOPPED)
        self.assertIn("stop:memory", self.events)
        self.assertIn("stop:providers", self.events)
        self.assertIn("stop:http", self.events)
        self.assertNotIn("start:writeback", self.events)

    async def test_completed_learning_handles_do_not_block_next_start(self) -> None:
        from fxbot.chat.memory.trimmed import TrimmedLearning

        learning = TrimmedLearning()
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        learning.tasks[("session", "expression")] = task
        learning.ingest_tasks["empty-session"] = set()
        learning.start()
        self.assertEqual(learning.tasks, {})
        self.assertEqual(learning.ingest_tasks, {})

    async def test_cleanup_failure_does_not_skip_provider_or_http_close(self) -> None:
        await self.app.start()
        self.failure = "stop:memory"
        with self.assertRaisesRegex(RuntimeError, "stop:memory"):
            await self.app.stop()
        self.assertIn("stop:providers", self.events)
        self.assertIn("stop:http", self.events)
        self.assertEqual(self.app.state, ApplicationState.STOPPED)


class StandaloneChatTests(unittest.TestCase):
    """不调用真实模型、不初始化外部框架，经真实 WebSocket 帧完成 AI 回复。"""

    def test_ai_route_model_reply_and_repeated_application_lifecycle(self) -> None:
        config = deepcopy(SYSTEM_DEFAULTS)
        config["chat"]["provider"] = "fixture"
        config["chat"]["providers"] = {"fixture": {"type": "fixture", "provider_type": "chat", "model": "fixture"}}
        config["chat"]["response_splitter"]["enable"] = False
        instances = []
        mode = {"direct": True}

        class FixtureProvider(ChatProvider):
            """只返回固定文字，不打开网络连接。"""

            def __init__(self, provider_id, config):
                super().__init__(provider_id, config)
                self.calls = []
                self.closed = False
                self.planned = False
                instances.append(self)

            async def chat(self, messages, *args, **kwargs):
                self.calls.append(messages)
                if any(item["function"]["name"] == "reply" for item in kwargs.get("tools") or []):
                    name = "stop" if self.planned else "reply"
                    self.planned = True
                    arguments = {} if name == "stop" else {"msg_id": "19002", "reply_reference": "回答用户"}
                    return LLMResponse(
                        tool_calls=[
                            {
                                "id": name,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments, ensure_ascii=False),
                                },
                            }
                        ]
                    )
                return LLMResponse(content="独立回复")

            async def close(self):
                self.closed = True

        unregister = register_session_policy(
            "standalone-fixture",
            lambda _: SessionPolicy(
                direct_reply=mode["direct"],
                allow_debounce=False,
                allow_typing=False,
                allow_typo=False,
            ),
        )
        self.addCleanup(unregister)
        with (
            patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(config)),
            patch.object(provider_manager, "_create_provider", side_effect=FixtureProvider),
            patch("fxbot.chat.message_receive.sender._should_quote", return_value=False),
        ):
            for index in range(2):
                mode["direct"] = index == 0
                app = create_app(Settings("test-only"))
                with TestClient(app) as client:
                    self.assertTrue(state.is_ready())
                    self.assertTrue(any(job.name == "chat:lifecycle" for job in app.state.services.scheduler.jobs))
                    with client.websocket_connect("/onebot/v11/ws", headers=dict(HEADERS)) as websocket:
                        incoming = payload()
                        incoming.update(
                            message_type="private",
                            sub_type="friend",
                            message_id=19001 + index,
                            message=[{"type": "text", "data": {"text": "你好"}}],
                        )
                        websocket.send_json(incoming)
                        action = websocket.receive_json()
                        self.assertEqual(action["action"], "send_private_msg")
                        self.assertEqual(action["params"]["message"], [{"type": "text", "data": {"text": "独立回复"}}])
                        websocket.send_json(
                            {
                                "echo": action["echo"],
                                "status": "ok",
                                "retcode": 0,
                                "data": {"message_id": 20001 + index},
                            }
                        )
                self.assertEqual(app.state.runtime.state, ApplicationState.STOPPED)
                self.assertFalse(state.is_ready())
                self.assertTrue(instances[-1].closed)
        self.assertEqual(len(instances), 2)
        self.assertTrue(instances[1].planned)
        self.assertEqual(instances[0].calls[0][-1], {"role": "user", "content": "你好"})
