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
from unittest.mock import Mock, AsyncMock, patch

import fxbot.plugins.server_admin as plugin
import fxbot.plugins.server_admin.store as store
import fxbot.plugins.server_admin.access as access
import fxbot.plugins.server_admin.config as config
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
        data = {"blocked_commands": [r"\brm\s+-rf\s+/", r"["], "auto_run_commands": ["ls"]}
        rules = SimpleNamespace(load=Mock(return_value=data), _last_error=None)
        with patch.object(config, "REG", rules):
            self.assertNotEqual(config.blocked_reason("rm -rf /"), "")
            self.assertEqual(config.blocked_reason("ls -la"), "")
        broken = SimpleNamespace(load=Mock(return_value={}), _last_error="bad config")
        with patch.object(config, "REG", broken):
            self.assertEqual(config.blocked_reason("ls"), "命令黑名单配置不可用")
            self.assertEqual(config.auto_run_commands(), [])
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
