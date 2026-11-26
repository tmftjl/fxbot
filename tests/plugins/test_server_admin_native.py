"""服务器运维原生回归：权限、命令白名单、执行边界、会话退出与进程清理。"""

from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
import importlib
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone
from contextlib import ExitStack
from unittest.mock import Mock, AsyncMock, patch

import fxbot.plugins.server_admin as plugin
import fxbot.plugins.server_admin.store as store
import fxbot.plugins.server_admin.access as access
import fxbot.plugins.server_admin.config as config
import fxbot.plugins.server_admin.policy as policy
import fxbot.plugins.server_admin.cc_store as cc_store
import fxbot.plugins.server_admin.cc_service as cc_service
from fxbot.core import Event, Message, SessionTarget
from fxbot.core.application import Application, ApplicationState

# 包内 __init__ 把同名执行器实例导出为 `executor`，显式取回模块用于打补丁。
executor = importlib.import_module("fxbot.plugins.server_admin.executor")

PLATFORM = "onebot11"
BOT_ID = "100"
USER_ID = "200"
GROUP_ID = "300"
BASE = f"{PLATFORM}:{BOT_ID}:private:{USER_ID}"
SERVER = f"{PLATFORM}:{BOT_ID}:server:private:{USER_ID}"
BOT_ADMIN = f"{PLATFORM}:{BOT_ID}:private:201"


class BotStub:
    platform = PLATFORM
    bot_id = BOT_ID


def make_event(
    kind: str = "private",
    text: str = "",
    *,
    user_id: str = USER_ID,
    group_id: str = GROUP_ID,
    metadata: dict | None = None,
    message_id: str = "m1",
    event_id: str = "e1",
) -> Event:
    target = SessionTarget(PLATFORM, BOT_ID, kind, group_id if kind == "group" else user_id)
    return Event(
        event_id=event_id,
        message_id=message_id,
        event_type="message",
        platform=PLATFORM,
        bot_id=BOT_ID,
        user_id=user_id,
        user_name="tester",
        target=target,
        message=Message.text(text),
        metadata={} if metadata is None else metadata,
        occurred_at=datetime.now(timezone.utc),
    )


class AccessTests(unittest.TestCase):
    def test_only_configured_superuser_enters_and_bot_admin_is_rejected(self):
        private = make_event()
        bot_admin = make_event(user_id="201")
        group = make_event("group")
        group_bot_admin = make_event("group", user_id="201")
        with (
            patch.object(access, "plugin_enabled", return_value=True),
            patch.object(access, "_system", return_value={"superusers": [BASE], "bot_admins": [BOT_ADMIN]}),
        ):
            self.assertTrue(access.owner_allowed(BotStub(), private))
            self.assertFalse(access.owner_allowed(BotStub(), bot_admin))
            self.assertFalse(access.owner_allowed(BotStub(), group))
            self.assertTrue(access.owner_allowed_group(BotStub(), group))
            self.assertFalse(access.owner_allowed_group(BotStub(), group_bot_admin))
            self.assertFalse(access.owner_allowed_group(BotStub(), private))

    def test_plugin_switch_requires_core_permission_node_and_command_enable(self):
        def document(command_enabled: bool = True, plugin_enabled: bool = True) -> dict:
            return {
                "top": {"enabled": True},
                "sub_plugins": {
                    "server_admin": {
                        "top": {"enabled": plugin_enabled},
                        "commands": {"mode": {"enabled": command_enabled}},
                    }
                },
            }

        for data, expected in (
            (document(), True),
            (document(command_enabled=False), False),
            (document(plugin_enabled=False), False),
            ({"top": {"enabled": True}, "sub_plugins": {}}, False),
        ):
            storage = SimpleNamespace(load=Mock(return_value=data))
            with patch.object(access, "get_storage", return_value=storage):
                self.assertIs(access.plugin_enabled(), expected)

    def test_execution_boundary_binds_real_event_and_derived_session(self):
        private = make_event()
        marked_group = make_event("group", metadata={access.GROUP_EVENT_ATTR: True})

        with (
            patch.object(access, "plugin_enabled", return_value=True),
            patch.object(access, "enabled", return_value=True),
            patch.object(access, "_system", return_value={"superusers": [BASE]}),
        ):
            self.assertTrue(access.execution_allowed(SERVER, BotStub(), private, USER_ID, None))
            self.assertFalse(access.execution_allowed(SERVER, BotStub(), private, "999", None))
            self.assertFalse(access.execution_allowed(SERVER, BotStub(), private, USER_ID, GROUP_ID))
            self.assertFalse(access.execution_allowed(BASE, BotStub(), private, USER_ID, None))
            self.assertTrue(access.execution_allowed(SERVER, BotStub(), marked_group, USER_ID, None))
            self.assertFalse(access.execution_allowed(SERVER, BotStub(), marked_group, USER_ID, GROUP_ID))

    def test_unmarked_group_event_cannot_use_server_exec(self):
        group = make_event("group")
        with (
            patch.object(access, "plugin_enabled", return_value=True),
            patch.object(access, "enabled", return_value=True),
            patch.object(access, "_system", return_value={"superusers": [BASE]}),
        ):
            self.assertFalse(access.execution_allowed(SERVER, BotStub(), group, USER_ID, None))
            access.mark_group_event(group)
            self.assertTrue(access.execution_allowed(SERVER, BotStub(), group, USER_ID, None))

    def test_is_superuser_reads_core_system_config_not_environment(self):
        with patch.object(access, "_system", return_value={"superusers": [BASE]}):
            self.assertTrue(access.is_superuser(BASE))
            self.assertFalse(access.is_superuser(BOT_ADMIN))
            self.assertFalse(access.is_superuser(""))


class StoreSessionTests(unittest.TestCase):
    def test_session_naming_keeps_private_identity_and_rejects_groups(self):
        self.assertEqual(store.base_session(SERVER), BASE)
        self.assertEqual(store.server_session(BASE), SERVER)
        self.assertTrue(store.is_server_session(SERVER))
        self.assertFalse(store.is_server_session(BASE))
        with self.assertRaises(ValueError):
            store.server_session(f"{PLATFORM}:{BOT_ID}:group:{GROUP_ID}")

    def test_mode_storage_keeps_original_config_format(self):
        class FakeProxy:
            def __init__(self) -> None:
                self.data = {"sessions": {}}
                self._last_error = None

            def load(self) -> dict:
                return json.loads(json.dumps(self.data))

            def save(self, data: dict) -> None:
                self.data = json.loads(json.dumps(data))

        proxy = FakeProxy()
        with (
            patch.object(store, "_proxy", return_value=proxy),
            patch.object(store, "_sync_persona"),
            patch.object(store, "get_binding", return_value=""),
            patch.object(store, "set_binding") as binding,
        ):
            store.set_mode(BASE, True)
            self.assertEqual(proxy.data, {"sessions": {BASE: {"enabled": True}}})
            self.assertTrue(store.enabled(SERVER))
            store.set_mode(SERVER, False)
            self.assertEqual(proxy.data, {"sessions": {BASE: {"enabled": False}}})
            self.assertFalse(store.enabled(SERVER))
        binding.assert_called_once_with("persona", SERVER, "运维")


class CommandPolicyTests(unittest.TestCase):
    def setUp(self):
        self.application = Mock(spec=Application, state=ApplicationState.RUNNING)

    def test_read_only_prefixes_and_metacharacters_require_confirmation(self):
        allow = ["pwd", "ls", "grep", "hostname", "date", "journalctl"]
        self.assertFalse(executor.needs_confirmation("pwd", allow))
        self.assertTrue(executor.needs_confirmation("ls | grep x", allow))
        self.assertTrue(executor.needs_confirmation("journalctl --unit=x", allow))
        self.assertFalse(executor.needs_confirmation("journalctl --unit=x", ["journalctl --unit=x"]))
        for command in (
            "pwd; id",
            "pwd && id",
            "echo $(id)",
            "cat file > out",
            "date -s 2020-01-01",
            "journalctl --vacuum-time=1d",
            "hostname evil",
            "pwd | id",
            "make install",
            "env sh -c 'touch file'",
            "git diff --output=file a b",
        ):
            with self.subTest(command=command):
                self.assertTrue(executor.needs_confirmation(command, allow))

    def test_blocked_commands_and_unreadable_config_fail_closed(self):
        data = {"blocked_commands": [r"\brm\s+-rf\s+/", r"["], "confirmed_commands": ["ls"]}
        rules = SimpleNamespace(load=Mock(return_value=data), _last_error=None)
        with patch.object(config, "REG", rules):
            self.assertNotEqual(config.blocked_reason("rm -rf /"), "")
            self.assertEqual(config.blocked_reason("ls -la"), "")
        broken = SimpleNamespace(load=Mock(return_value={}), _last_error="bad config")
        with patch.object(config, "REG", broken):
            self.assertEqual(config.blocked_reason("ls"), "命令黑名单配置不可用")
            self.assertEqual(config.confirmed_commands(), [])
            self.assertFalse(config.skip_confirmation())

    def test_pending_command_requires_second_event_confirmation(self):
        manager = executor.CommandExecutor()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(executor, "audit"):
                result = manager.submit(
                    SERVER, "make install", directory, lambda: True, allow=[], application=self.application
                )
                self.assertEqual(result["status"], "pending")
                manager.presented(SERVER, result["job_id"], "source-a")
                with self.assertRaises(ValueError):
                    manager.confirm(SERVER, "source-a", "确认")
                with self.assertRaises(ValueError):
                    manager.confirm("other", "source-b", "确认")
                with patch.object(manager, "_start") as start:
                    confirmed = manager.confirm(SERVER, "source-b", "确认")
                self.assertEqual(confirmed["job_id"], result["job_id"])
                start.assert_called_once()

    def test_whitelisted_command_starts_without_confirmation_but_rejects_bad_input(self):
        manager = executor.CommandExecutor()
        with tempfile.TemporaryDirectory() as directory, patch.object(executor, "audit"):
            with patch.object(manager, "_start") as start:
                manager.submit(SERVER, "pwd", directory, lambda: True, allow=["pwd"], application=self.application)
                start.assert_called_once()
            with self.assertRaises(ValueError):
                manager.submit(SERVER, "", directory, lambda: True, allow=[], application=self.application)
            with self.assertRaises(ValueError):
                manager.submit(SERVER, "pwd", "relative", lambda: True, allow=[], application=self.application)
            with self.assertRaises(PermissionError):
                manager.submit(SERVER, "pwd", directory, lambda: False, allow=[], application=self.application)

    def test_permanent_reply_is_accepted_and_distinguished(self):
        manager = executor.CommandExecutor()
        with tempfile.TemporaryDirectory() as directory, patch.object(executor, "audit"):
            job = manager.submit(
                SERVER, "make install", directory, lambda: True, allow=[], application=self.application
            )
            manager.presented(SERVER, job["job_id"], "first")
            self.assertTrue(executor.is_permanent_reply("确认执行 永久！"))
            self.assertTrue(executor.is_permanent_reply("以后都允许"))
            self.assertFalse(executor.is_permanent_reply("确认执行"))
            with patch.object(manager, "_start") as start:
                manager.confirm(SERVER, "second", "确认执行 永久")
                start.assert_called_once()

    def test_permanent_allow_commands_are_persisted_once(self):
        data = {"confirmed_commands": ["ls"]}

        class FakeProxy:
            _last_error = None

            def load(self):
                return json.loads(json.dumps(data))

            def save(self, value):
                data.clear()
                data.update(json.loads(json.dumps(value)))

        with patch.object(config, "REG", FakeProxy()):
            self.assertTrue(config.remember_command("make install"))
            self.assertTrue(config.remember_command("make install"))
            self.assertEqual(data["confirmed_commands"], ["ls", "make install"])
            self.assertFalse(config.remember_command("  "))

    def test_confirmation_rejects_negative_ambiguous_and_question_replies(self):
        manager = executor.CommandExecutor()
        with tempfile.TemporaryDirectory() as directory, patch.object(executor, "audit"):
            job = manager.submit(
                SERVER, "make install", directory, lambda: True, allow=[], application=self.application
            )
            manager.presented(SERVER, job["job_id"], "first")
            for reply in ("cancel", "不要执行", "确认？", "确认一下会不会删除文件", "等一下", "", "yes but not now"):
                with self.subTest(reply=reply), patch.object(manager, "_start") as start:
                    with self.assertRaises(PermissionError):
                        manager.confirm(SERVER, "second", reply)
                    start.assert_not_called()
                    self.assertEqual(manager.jobs[job["job_id"]].status, "pending")
            with patch.object(manager, "_start") as start:
                manager.confirm(SERVER, "second", "确认执行！")
                start.assert_called_once()


class ClaudeCodeSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_cc_store_roundtrip_and_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            with patch.object(cc_store, "_path", return_value=path):
                cc_store.put(SERVER, "sid-1")
                self.assertEqual(cc_store.get(SERVER), "sid-1")
                cc_store.drop(SERVER)
                self.assertEqual(cc_store.get(SERVER), "")

    async def test_open_session_prefers_resume_and_falls_back(self):
        session = cc_service.CcSession("probe", Path("work"))
        calls = []

        class Resumed:
            async def resume_session(self, **kwargs):
                calls.append("resume")
                raise RuntimeError("not supported")

            async def load_session(self, **kwargs):
                calls.append("load")
                return SimpleNamespace(session_id="restored")

            async def new_session(self, **kwargs):
                calls.append("new")
                return SimpleNamespace(session_id="fresh")

        with (
            patch.object(cc_service, "cc_keep_context", return_value=True),
            patch.object(cc_service.cc_store, "get", return_value="sticky"),
            patch.object(cc_service.cc_store, "put") as put,
        ):
            await session._open_session(Resumed())
        self.assertEqual(session.acp_session, "restored")
        self.assertEqual(calls, ["resume", "load"])
        put.assert_called_once_with("probe", "restored")

    async def test_open_session_creates_new_when_resume_fails(self):
        session = cc_service.CcSession("probe", Path("work"))

        class Broken:
            async def resume_session(self, **kwargs):
                raise RuntimeError("no")

            async def load_session(self, **kwargs):
                raise RuntimeError("no")

            async def new_session(self, **kwargs):
                return SimpleNamespace(session_id="fresh")

        with (
            patch.object(cc_service, "cc_keep_context", return_value=True),
            patch.object(cc_service.cc_store, "get", return_value="sticky"),
            patch.object(cc_service.cc_store, "put"),
        ):
            await session._open_session(Broken())
        self.assertEqual(session.acp_session, "fresh")

    async def test_close_drops_or_keeps_native_id(self):
        session = cc_service.CcSession("probe", Path("work"))
        session.acp_session = "sid"
        with patch.object(cc_service.cc_store, "drop") as drop, patch.object(cc_service.cc_store, "put") as put:
            await session.close(drop_native=True)
        drop.assert_called_once_with("probe")
        put.assert_not_called()

        session.acp_session = "sid"
        with patch.object(cc_service.cc_store, "drop") as drop, patch.object(cc_service.cc_store, "put") as put:
            await session.close(drop_native=False)
        drop.assert_not_called()
        put.assert_called_once_with("probe", "sid")


class PermissionPolicyTests(unittest.IsolatedAsyncioTestCase):
    def rules(self, mode="auto", confirmed=()):
        stack = ExitStack()
        stack.enter_context(patch.object(policy, "permission_mode", return_value=mode))
        stack.enter_context(patch.object(policy, "confirmed_commands", return_value=list(confirmed)))
        return stack

    def test_permanent_commands_run_exactly_and_never_widen(self):
        with self.rules(confirmed=["rm -rf /tmp/x"]):
            decision = policy.rule_decision("rm -rf /tmp/x")
            self.assertEqual((decision.action, decision.reason), ("allow", "已永久允许"))
            self.assertIsNone(policy.rule_decision("rm -rf /tmp/x2"))

    def test_mode_decisions_before_classifier(self):
        with self.rules(mode="bypass"):
            self.assertTrue(policy.rule_decision("rm -rf /tmp/x").auto_run)
        with self.rules(mode="confirm"):
            self.assertEqual(policy.rule_decision("ls").action, "ask")
        with self.rules(mode="auto"):
            self.assertIsNone(policy.rule_decision("ls"))

    def history(self, messages):
        return patch.object(policy, "_history", return_value=list(messages))

    async def test_fast_stage_allow_skips_review(self):
        answers = ["<block>no</block>"]
        with (
            self.rules(),
            self.history([{"role": "user", "content": "看下磁盘"}]),
            patch.object(policy, "_ask", AsyncMock(side_effect=answers)) as ask,
        ):
            decision = await policy.decide(session_id=SERVER, command="df -h", cwd=".", request_text="看下磁盘")
        self.assertTrue(decision.auto_run)
        self.assertEqual(ask.await_count, 1)
        kwargs = ask.await_args.kwargs
        self.assertEqual(kwargs["max_tokens"], 64)
        self.assertEqual(kwargs["stop"], ["</block>"])
        self.assertIn("## Session Context", kwargs["system"])

    async def test_fast_stage_blocks_then_review_decides(self):
        with (
            self.rules(),
            self.history([{"role": "user", "content": "看下磁盘"}]),
            patch.object(
                policy,
                "_ask",
                AsyncMock(
                    side_effect=[
                        "<block>yes</block>",
                        "<thinking>超出请求</thinking><block>no</block>",
                    ]
                ),
            ) as ask,
        ):
            allowed = await policy.decide(session_id=SERVER, command="df -h", cwd=".", request_text="看下磁盘")
        self.assertTrue(allowed.auto_run)
        self.assertEqual(ask.await_count, 2)
        self.assertEqual(ask.await_args_list[1].kwargs["max_tokens"], 4096)
        self.assertNotIn("stop", ask.await_args_list[1].kwargs)
        with (
            self.rules(),
            self.history([{"role": "user", "content": "看下磁盘"}]),
            patch.object(
                policy,
                "_ask",
                AsyncMock(
                    side_effect=[
                        "<block>yes</block>",
                        (
                            "<thinking>重启无关服务</thinking><block>yes</block>"
                            "<category>Scope</category><reason>[Scope] 超出请求</reason>"
                        ),
                    ]
                ),
            ),
        ):
            blocked = await policy.decide(
                session_id=SERVER, command="systemctl restart nginx", cwd=".", request_text="看下磁盘"
            )
        self.assertEqual(blocked.action, "ask")
        self.assertIn("[Scope] 超出请求", blocked.reason)

    async def test_unparseable_review_and_timeout_fall_back_to_ask(self):
        with (
            self.rules(),
            self.history([{"role": "user", "content": "看下磁盘"}]),
            patch.object(policy, "_ask", AsyncMock(side_effect=["<block>yes</block>", "随便说说"])),
        ):
            decision = await policy.decide(session_id=SERVER, command="df -h", cwd=".", request_text="看下磁盘")
        self.assertEqual(decision.action, "ask")
        with (
            self.rules(),
            patch.object(policy, "_classify", side_effect=asyncio.TimeoutError),
        ):
            decision = await policy.decide(session_id=SERVER, command="df -h", cwd=".", request_text="看下磁盘")
        self.assertEqual((decision.action, decision.reason), ("ask", "分类器超时"))

    async def test_missing_user_context_never_reaches_classifier(self):
        with self.rules(), self.history([]), patch.object(policy, "_ask", AsyncMock()) as ask:
            decision = await policy.decide(session_id=SERVER, command="make install", cwd=".", request_text="  ")
        self.assertEqual(decision.action, "ask")
        ask.assert_not_called()

    async def test_classifier_request_disables_thinking(self):
        captured = {}

        class FakeProvider:
            async def chat(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(content="<block>no</block>")

        class FakeTaskProvider:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return FakeProvider()

            async def __aexit__(self, *exc):
                return False

        with (
            patch("fxbot.chat.providers.routing.candidate_providers", return_value=["probe"]),
            patch("fxbot.chat.providers.routing.TaskProvider", FakeTaskProvider),
        ):
            text = await policy._ask(SERVER, "t", system="s", max_tokens=64, timeout=5, stop=["</block>"])
        self.assertEqual(text, "<block>no</block>")
        self.assertIs(captured["thinking"], False)
        self.assertEqual(captured["stop"], ["</block>"])
        self.assertEqual(captured["max_tokens"], 64)
        self.assertEqual(captured["temperature"], 0.0)

    async def test_transcript_too_long_blocks_without_calling_model(self):
        with (
            self.rules(),
            patch.object(policy, "build_transcript", return_value="x" * (policy._MAX_HISTORY_CHARS + 1)),
            patch.object(policy, "_ask", AsyncMock()) as ask,
        ):
            decision = await policy.decide(session_id=SERVER, command="df -h", cwd=".", request_text="看下磁盘")
        self.assertEqual(decision.action, "ask")
        self.assertIn("对话过长", decision.reason)
        ask.assert_not_called()

    def test_system_context_carries_environment(self):
        text = policy._system_content("/srv/app")
        self.assertTrue(text.startswith(policy.SYSTEM_PROMPT))
        self.assertIn("## Session Context", text)
        self.assertIn("- working directory: /srv/app", text)
        self.assertIn("- platform: ", text)

    def test_transcript_keeps_user_text_and_tool_calls_only(self):
        history = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "看下日志\n再重启服务"},
            {
                "role": "assistant",
                "content": "我准备执行命令",
                "tool_calls": [{"function": {"name": "server_exec", "arguments": '{"command": "tail -n 20 app.log"}'}}],
            },
            {"role": "tool", "name": "server_exec", "content": "log body"},
            {"role": "user", "content": "内部规划", "_planner_internal": True},
        ]
        with self.history(history):
            text = policy.build_transcript(SERVER, "systemctl restart api", "/srv", "看下日志 再重启服务")
        self.assertIn("User: 看下日志 再重启服务", text)
        self.assertIn('server_exec {"command":"tail -n 20 app.log"}', text)
        self.assertIn('Bash {"command":"systemctl restart api","cwd":"/srv"}', text)
        self.assertIn("Assistant: 我准备执行命令", text)
        self.assertNotIn("log body", text)
        self.assertNotIn("内部规划", text)

    def test_parse_block_and_reason(self):
        self.assertEqual(policy.parse_block("<block>YES</block><reason>x</reason>"), "yes")
        self.assertEqual(policy.parse_block("<block>no</block>"), "no")
        self.assertEqual(policy.parse_block("没有标签"), "")
        self.assertEqual(policy.parse_reason("<reason> 超出请求\n扩大范围 </reason>"), "超出请求 扩大范围")
        self.assertEqual(
            policy.parse_category("<category> Irreversible Local Destruction </category>"),
            "Irreversible Local Destruction",
        )
        self.assertEqual(policy.parse_category("没有标签"), "")


class ChatModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_mode_exits_and_cancels_session_jobs(self):
        event = make_event(text="#聊天模式")
        with (
            patch.object(plugin, "owner_allowed", return_value=True),
            patch.object(plugin.store, "set_mode") as set_mode,
            patch.object(plugin.executor, "cancel", return_value=True) as cancel,
            patch.object(plugin, "audit"),
        ):
            message = await plugin.handle_mode(BotStub(), event)
        set_mode.assert_called_once_with(BASE, False)
        cancel.assert_called_once_with(SERVER)
        self.assertIn("已退出服务器模式", message)

    async def test_server_mode_enables_and_audits_identity(self):
        event = make_event(text="#服务器模式")
        with (
            patch.object(plugin, "owner_allowed", return_value=True),
            patch.object(plugin.store, "set_mode") as set_mode,
            patch.object(plugin, "audit") as audit,
        ):
            message = await plugin.handle_mode(BotStub(), event)
        set_mode.assert_called_once_with(BASE, True)
        audit.assert_called_once_with("mode_enabled", SERVER, user_id=USER_ID)
        self.assertIn("服务器模式已开启", message)

    async def test_unauthorized_mode_command_is_ignored(self):
        with patch.object(plugin, "owner_allowed", return_value=False):
            self.assertEqual(await plugin.handle_mode(BotStub(), make_event(text="#服务器模式")), "")

    async def test_tool_entry_rejects_missing_authorization_and_allows_cancel(self):
        context = SimpleNamespace(user_id=USER_ID, group_id=None, session_id=SERVER)
        runtime = SimpleNamespace(bot=BotStub(), event=make_event(), sender=None, application=Mock(spec=Application))
        with patch.object(plugin, "execution_allowed", return_value=False):
            with self.assertRaises(plugin.ToolError) as denied:
                await plugin.server_exec(context, runtime, command="pwd")
        self.assertEqual(denied.exception.code, "server_admin_denied")
        with (
            patch.object(plugin, "execution_allowed", return_value=True),
            patch.object(plugin.executor, "cancel", return_value=True) as cancel,
        ):
            self.assertEqual(await plugin.server_exec(context, runtime, cancel=True), {"cancelled": True})
        cancel.assert_called_once_with(SERVER)


class ClaudeCodeUtilityTests(unittest.IsolatedAsyncioTestCase):
    def test_collect_attachments_stays_inside_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inside = root / "report.md"
            inside.write_text("ok", encoding="utf-8")
            outside = Path(tempfile.gettempdir()) / "cc-outside.txt"
            outside.write_text("no", encoding="utf-8")
            try:
                found = cc_service._collect_attachments(f"见 {inside} 和 {outside}", root)
                self.assertEqual(found, [inside.resolve()])
            finally:
                outside.unlink(missing_ok=True)

    def test_wipe_keeps_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("x", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "b.txt").write_text("y", encoding="utf-8")
            self.assertEqual(cc_service._wipe(root), 2)
            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])

    def test_doctor_reports_binaries(self):
        with patch.object(cc_service.shutil, "which", side_effect=lambda name: f"/bin/{name}"):
            text = cc_service.manager.doctor()
        self.assertIn("node: /bin/node", text)
        self.assertIn("npx: /bin/npx", text)
        self.assertIn("claude: npx -y @agentclientprotocol/claude-agent-acp@0.112.0", text)
        self.assertIn("codex: npx -y @agentclientprotocol/codex-acp@2.0.1", text)

    async def test_resume_records_pending_session(self):
        manager = cc_service.CcManager()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(cc_service, "cc_workdir", return_value=Path(directory)),
                patch.object(cc_service.cc_store, "put") as put,
            ):
                message = await manager.resume("probe", "sid-1")
                self.assertIn("sid-1", message)
                put.assert_called_once_with("probe|claude", "sid-1")
                self.assertTrue(manager.get("probe")._force_resume)


class ClaudeCodeHelpTests(unittest.IsolatedAsyncioTestCase):
    async def test_cc_help_uses_plugin_help_image(self):
        context = SimpleNamespace(send=AsyncMock())
        with patch("fxbot.plugins.help.help_image", AsyncMock(return_value=b"png")):
            await plugin._send_cc_help(context)
        message = context.send.await_args.args[0]
        self.assertEqual(message.segments[0].type, "image")
        self.assertEqual(message.segments[0].data["file"], b"png")

    async def test_cc_help_falls_back_to_text(self):
        context = SimpleNamespace(send=AsyncMock())
        with patch("fxbot.plugins.help.help_image", AsyncMock(return_value=b"")):
            await plugin._send_cc_help(context)
        text = context.send.await_args.args[0]
        self.assertIn("cc <任务>", text)


class ClaudeCodeDetailTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_handles_pending_requests_in_order(self):
        session = cc_service.CcSession("probe", Path("work"))
        first = SimpleNamespace(kind="allow_once", option_id="a1")
        reject = SimpleNamespace(kind="reject_once", option_id="r1")
        future = asyncio.get_event_loop().create_future()
        session._pendings.append((future, [first, reject]))
        self.assertEqual(session.resolve("reject_once"), "已拒绝。")
        self.assertEqual(future.result(), "r1")
        self.assertEqual(session.resolve("allow_once"), "当前没有待审批的请求。")

    def test_resolve_reports_offered_kinds(self):
        session = cc_service.CcSession("probe", Path("work"))
        future = asyncio.get_event_loop().create_future()
        session._pendings.append((future, [SimpleNamespace(kind="reject_once", option_id="r1")]))
        message = session.resolve("allow_once")
        self.assertIn("reject_once", message)
        self.assertFalse(future.done())

    def test_tool_summary_compacts_content(self):
        block = SimpleNamespace(content=SimpleNamespace(text="Bash\n执行了命令"))
        update = SimpleNamespace(content=[block])
        self.assertEqual(cc_service._tool_summary(update), "Bash 执行了命令")

    async def test_busy_behavior_reject_and_queue(self):
        manager = cc_service.CcManager()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(cc_service, "cc_workdir", return_value=Path(directory)),
                patch.object(cc_service.cc_store, "put"),
            ):
                session = manager.get("probe")
                session.busy = True
                with patch.object(cc_service, "cc_busy_behavior", return_value="reject"):
                    ok, message = await manager.start("probe", "x", AsyncMock())
                self.assertFalse(ok)
                self.assertIn("上一轮", message)
                with patch.object(cc_service, "cc_busy_behavior", return_value="queue"):
                    ok, message = await manager.start("probe", "x", AsyncMock())
                self.assertTrue(ok)
                self.assertIn("排队", message)

    def test_engine_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engines.json"
            with patch.object(cc_store, "_engine_path", return_value=path):
                cc_store.put_engine("probe", "codex")
                self.assertEqual(cc_store.get_engine("probe"), "codex")

    async def test_switch_engine_persists_choice(self):
        manager = cc_service.CcManager()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(cc_service, "cc_workdir", return_value=Path(directory)),
                patch.object(cc_service.cc_store, "put_engine") as put_engine,
            ):
                message = await manager.switch_engine("probe", "codex")
        self.assertIn("codex", message)
        put_engine.assert_called_once_with("probe", "codex")

    async def test_queue_list_and_dequeue(self):
        manager = cc_service.CcManager()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cc_service, "cc_workdir", return_value=Path(directory)):
                session = manager.get("probe")
                session.busy = True
                await manager.start("probe", "第一条", AsyncMock())
                await manager.start("probe", "第二条", AsyncMock())
                info = manager.queue_info("probe")
                self.assertIn("第一条", info)
                self.assertIn("第二条", info)
                message = await manager.dequeue("probe", "1")
                self.assertIn("剩余 1", message)
                self.assertNotIn("第一条", manager.queue_info("probe"))

    async def test_cancel_denies_pending_approvals(self):
        session = cc_service.CcSession("probe", Path("work"))
        session.busy = True
        session.acp_session = "sid"
        session.conn = SimpleNamespace(cancel=AsyncMock())
        future = asyncio.get_event_loop().create_future()
        session._pendings.append((future, []))
        message = await session.cancel()
        self.assertIn("打断", message)
        self.assertIsNone(future.result())

    def test_pid_record_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pids.json"
            with patch.object(cc_service, "_pid_path", return_value=path):
                cc_service._record_pid(1234)
                self.assertEqual(cc_service._load_pids(), {"1234": "claude-agent-acp"})
                cc_service._forget_pid(1234)
                self.assertEqual(cc_service._load_pids(), {})


class ClaudeCodeRenderTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_blocks_carry_text_and_images(self):
        blocks = cc_service._prompt_blocks("看一下", ["data:image/png;base64,QUJD"])
        self.assertEqual(getattr(blocks[0], "type"), "text")
        self.assertEqual(getattr(blocks[1], "type"), "image")
        self.assertEqual(blocks[1].mime_type, "image/png")
        self.assertEqual(cc_service._image_bytes(blocks[1]), b"ABC")

    def test_usage_footer_accepts_dict_usage(self):
        response = SimpleNamespace(usage={"input_tokens": 10, "output_tokens": 20}, stop_reason="end_turn")
        self.assertEqual(cc_service._usage_footer(response), "in 10 · out 20 · end_turn")
        self.assertEqual(cc_service._usage_footer(SimpleNamespace()), "")

    async def test_image_mode_renders_pngs_instead_of_text(self):
        session = cc_service.CcSession("probe", Path("work"))
        images: list[bytes] = []
        texts: list[str] = []

        async def send_text(text):
            texts.append(text)

        async def send_image(data):
            images.append(data)

        session.attach(send_text, image=send_image)
        session._markdown.append("# 结果")
        session._tool_notes.append(("execute", "Bash 已执行", ""))
        with (
            patch.object(cc_service, "cc_output_format", return_value="image"),
            patch.object(cc_service, "cc_render_scale", return_value=1),
            patch.object(
                cc_service.cc_render,
                "render_to_pngs",
                AsyncMock(return_value=[b"png-1", b"png-2"]),
            ) as render,
        ):
            await session._deliver_output(SimpleNamespace(usage={"input_tokens": 5}, stop_reason="end_turn"))
        self.assertEqual(images, [b"png-1", b"png-2"])
        render.assert_awaited_once()
        self.assertFalse(texts)

    async def test_text_mode_sends_markdown_directly(self):
        session = cc_service.CcSession("probe", Path("work"))
        texts: list[str] = []

        async def send_text(text):
            texts.append(text)

        session.attach(send_text)
        session._markdown.append("hello")
        with patch.object(cc_service, "cc_output_format", return_value="text"):
            await session._deliver_output(SimpleNamespace())
        self.assertEqual(texts, ["hello"])


class ClaudeCodeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_allow_commands_resolve_permission(self):
        event = make_event()
        cases = (
            ("allow", "allow_once"),
            ("allow always", "allow_always"),
            ("deny", "reject_once"),
            ("允许", "allow_once"),
            ("允许 永久", "allow_always"),
            ("拒绝", "reject_once"),
        )
        for text, kind in cases:
            context = SimpleNamespace(
                event=event,
                bot=BotStub(),
                match=SimpleNamespace(groups=(text,)),
                send=AsyncMock(),
            )
            with patch.object(plugin.cc_manager, "resolve", return_value="ok") as resolve:
                await plugin._handle_cc(context)
            self.assertEqual(resolve.call_args.args[1], kind)
            context.send.assert_awaited_once_with("ok")


class _Stream:
    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self.chunks = list(chunks)
        self.delay = delay

    async def read(self, size: int) -> bytes:
        del size
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.chunks.pop(0) if self.chunks else b""


class _Stdin:
    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeProcess:
    pid = 12345

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        returncode: int = 0,
        read_delay: float = 0.0,
        wait_delay: float = 0.0,
    ) -> None:
        self.stdout = _Stream(chunks or [], read_delay)
        self.stdin = _Stdin()
        self.returncode: int | None = None
        self.final_returncode = returncode
        self.wait_delay = wait_delay
        self.killed = False

    async def wait(self) -> int:
        if self.wait_delay:
            await asyncio.sleep(self.wait_delay)
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeWindowsJob:
    close_called = False
    assign_error: Exception | None = None

    def assign(self, pid: int) -> None:
        del pid
        if self.assign_error is not None:
            raise self.assign_error

    def close(self) -> None:
        type(self).close_called = True


class ExecutorRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.application = Application()
        await self.application.start()
        self.addAsyncCleanup(self.application.stop)

    def setUp(self) -> None:
        FakeWindowsJob.close_called = False
        FakeWindowsJob.assign_error = None
        self.manager = executor.CommandExecutor()
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    async def _run_job(self, process: FakeProcess | None, *, job_cls=FakeWindowsJob, timeout: float | None = None):
        job = executor.Command(SERVER, "echo hi", str(self.root), lambda: True, self.application)
        self.manager.jobs[job.id] = job
        patches = [
            patch.object(executor, "audit"),
            patch.object(executor.os, "name", "nt"),
            patch.object(executor.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)),
            patch.object(executor.asyncio, "create_subprocess_shell", AsyncMock(return_value=process)),
            patch("fxbot.plugins.server_admin.windows_job.WindowsJob", job_cls),
        ]
        if timeout is not None:
            patches.append(patch.object(executor, "COMMAND_TIMEOUT", timeout))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.manager._start(job)
        await asyncio.wait_for(job.task, timeout=3)
        return job

    async def test_application_stop_cancels_and_awaits_command_cleanup(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run(job):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                job.status = "cancelled"

        with patch.object(executor, "audit"), patch.object(self.manager, "_run", run):
            result = self.manager.submit(
                SERVER, "pwd", str(self.root), lambda: True, allow=["pwd"], application=self.application
            )
            await entered.wait()
            stopping = asyncio.create_task(self.application.stop())
            try:
                await asyncio.wait_for(cleaning.wait(), 1)
                self.assertFalse(stopping.done())
            finally:
                release.set()
                await stopping
            job = self.manager.jobs[result["job_id"]]
            self.assertTrue(job.task.done())
            self.assertEqual(job.status, "cancelled")

    async def test_stopped_application_cannot_confirm_its_pending_command(self):
        with patch.object(executor, "audit"):
            result = self.manager.submit(
                SERVER, "make install", str(self.root), lambda: True, allow=[], application=self.application
            )
            self.manager.presented(SERVER, result["job_id"], "first")
            await self.application.stop()
            with self.assertRaises(ValueError):
                self.manager.confirm(SERVER, "second", "确认执行")
            self.assertEqual(self.manager.jobs[result["job_id"]].status, "cancelled")

    async def test_output_is_drained_but_truncated(self):
        process = FakeProcess([b"x" * (executor.OUTPUT_LIMIT * 10), b""])
        job = await self._run_job(process)
        self.assertEqual(job.status, "completed")
        self.assertTrue(job.truncated)
        self.assertLessEqual(len(job.output), executor.OUTPUT_LIMIT)

    async def test_timeout_kills_whole_process_and_marks_timeout(self):
        process = FakeProcess([b"x"], read_delay=1.0, wait_delay=0.2)
        job = await self._run_job(process, timeout=0.05)
        self.assertEqual(job.status, "timeout")
        self.assertTrue(process.killed)

    async def test_windows_job_creation_failure_does_not_spawn_shell(self):
        class FailingJob:
            def __init__(self) -> None:
                raise OSError("job unavailable")

        process = FakeProcess([])
        job = await self._run_job(process, job_cls=FailingJob)
        self.assertEqual(job.status, "failed")
        self.assertFalse(process.killed)

    async def test_windows_job_assignment_failure_still_closes_job_and_kills_process(self):
        class AssignFailingJob(FakeWindowsJob):
            assign_error = OSError("assign failed")

        process = FakeProcess([], wait_delay=0.05)
        job = await self._run_job(process, job_cls=AssignFailingJob)
        self.assertEqual(job.status, "failed")
        self.assertTrue(AssignFailingJob.close_called)
        self.assertTrue(process.killed)
