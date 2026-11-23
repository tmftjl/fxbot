"""核心进程命令的权限、重启标记与更新边界。"""

from __future__ import annotations

import sys
import json
import time
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

from fxbot.core import Event, Message, MessageSegment, ReplyTarget, SessionTarget, HandlerContext
from fxbot.core.application import Application
from fxbot.core.matching import MatchResult, regex_match
from fxbot.permission.policy import FeaturePolicy
from fxbot.permission.types import PermLevel, PermContext, perm_entry_default
from fxbot.runtime import processes, system_commands as system


class StubConfig:
    def __init__(self, superusers: list[str]) -> None:
        self.superusers = superusers

    def get_system(self) -> dict:
        return {"system": {"superusers": self.superusers}}


def make_event(*, platform="onebot11", bot_id="100", kind="group", target="300", user="200"):
    return SimpleNamespace(
        platform=platform,
        bot_id=bot_id,
        user_id=user,
        target=SessionTarget(platform, bot_id, kind, target),
    )


class SystemCommandTests(unittest.IsolatedAsyncioTestCase):
    def successful_pull(self, output="updated"):
        async def pull(on_success):
            on_success()
            return True, output, []

        return AsyncMock(side_effect=pull)

    async def test_successful_pull_restarts_when_report_fails_or_is_cancelled(self):
        for error in (TimeoutError(), RuntimeError("offline"), asyncio.CancelledError()):
            with (
                patch.object(system, "_git_pull", self.successful_pull()),
                patch.object(system, "_send_report", AsyncMock(side_effect=error)),
                patch.object(system, "_request_exit") as exit_process,
            ):
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await system._update(self.context())
                else:
                    await system._update(self.context())
                exit_process.assert_called_once()
                self.assertIsNotNone(system._PENDING_ARGV)
                self.assertTrue(self.flag.exists())

    async def test_restart_intent_exists_before_sending_update_report(self):
        async def report(context, lines):
            self.assertTrue(self.flag.exists())
            self.assertIsNotNone(system._PENDING_ARGV)

        with (
            patch.object(system, "_git_pull", self.successful_pull()),
            patch.object(system, "_send_report", report),
            patch.object(system, "_request_exit") as exit_process,
        ):
            await system._update(self.context())
            exit_process.assert_called_once()

    async def test_missing_git_is_a_reported_failure(self):
        with patch.object(
            system.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("git missing"))
        ):
            ok, detail, logs = await system._git_pull(Mock())
        self.assertFalse(ok)
        self.assertIn("git missing", detail)
        self.assertEqual(logs, [])

    async def test_metadata_failure_does_not_undo_successful_pull(self):
        with (
            patch.object(system, "_git_head", AsyncMock(side_effect=["before", TimeoutError()])),
            patch.object(system, "_run_git", AsyncMock(return_value=(0, "updated"))) as git,
        ):
            ok, detail, logs = await system._git_pull(Mock())
        self.assertTrue(ok)
        self.assertEqual(detail, "updated")
        self.assertTrue(logs)
        self.assertEqual(git.await_count, 1)

    async def test_cancel_after_pull_commit_still_requests_restart(self):
        with (
            patch.object(system, "_git_head", AsyncMock(side_effect=["before", asyncio.CancelledError()])),
            patch.object(system, "_run_git", AsyncMock(return_value=(0, "updated"))),
            patch.object(system, "_request_exit") as exit_process,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await system._update(self.context())
            self.assertTrue(self.flag.exists())
            self.assertIsNotNone(system._PENDING_ARGV)
            exit_process.assert_called_once()

    async def test_slow_update_report_is_bounded_and_cancelled_before_exit(self):
        cleaned = asyncio.Event()

        async def report(context, lines):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with (
            patch.object(system, "_git_pull", self.successful_pull()),
            patch.object(system, "_send_report", report),
            patch.object(system, "_REPORT_TIMEOUT_SECONDS", 0.01),
            patch.object(system, "_request_exit") as exit_process,
        ):
            await system._update(self.context())
            self.assertTrue(cleaned.is_set())
            exit_process.assert_called_once()

    async def test_git_timeout_and_cancel_reclaim_the_owned_process(self):
        for cancel in (False, True):
            entered, released = asyncio.Event(), asyncio.Event()
            process = Mock(pid=123, returncode=None)

            async def communicate():
                entered.set()
                await released.wait()
                return b"", b""

            process.communicate = AsyncMock(side_effect=communicate)

            def terminate(child):
                child.returncode = -1
                released.set()

            with (
                patch.object(system.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn,
                patch.object(system, "terminate_process", side_effect=terminate) as kill,
            ):
                task = asyncio.create_task(system._run_git("rev-parse", "HEAD", timeout=0.02))
                await entered.wait()
                if cancel:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                    await task
                kill.assert_called_once_with(process)
                self.assertEqual(spawn.call_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)
                self.assertEqual(process.communicate.await_count, 1)

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.flag = Path(directory.name) / "restart_flag.json"
        patcher = patch.object(system, "_FLAG_FILE", self.flag)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, system, "_PENDING_ARGV", None)
        self.enterContext(patch.object(system, "_docker_deployment", return_value=False))
        # 默认按“依赖未变化”走纯代码更新；需要同步的用例单独把指纹改成变化。
        self.enterContext(patch.object(system, "_deps_changed", return_value=False))
        self.event = make_event()

    def context(self, bot=None) -> HandlerContext:
        bot = bot or SimpleNamespace(platform="onebot11", bot_id="100", send=AsyncMock(), capabilities=())
        return HandlerContext(bot, self.event, MatchResult(""))

    async def test_superuser_accepts_full_key_or_raw_user_id(self) -> None:
        check = system.superuser_check(StubConfig(["onebot11:100:private:200"]))
        self.assertTrue(await check(self.event))
        self.assertFalse(await check(make_event(user="201")))
        self.assertFalse(await check(make_event(platform="qq")))
        bare = system.superuser_check(StubConfig(["200"]))
        self.assertTrue(await bare(self.event))
        self.assertTrue(await bare(make_event(platform="qq")))
        self.assertFalse(await bare(make_event(user="201")))
        self.assertFalse(await system.superuser_check(StubConfig([]))(self.event))

    async def test_restart_writes_target_and_schedules_reexec(self) -> None:
        context = self.context()
        with patch.object(system, "_request_exit") as exit_process:
            await system._restart(context)
        exit_process.assert_called_once()
        self.assertEqual(system._PENDING_ARGV[:3], [sys.executable, "-m", "fxbot"])
        data = json.loads(self.flag.read_text(encoding="utf-8"))
        self.assertEqual(data["target"], "onebot11:100:group:300")
        self.assertEqual(data["message"], "FxBot 重启完成")

    async def test_shutdown_does_not_schedule_reexec(self) -> None:
        context = self.context()
        with patch.object(system, "_request_exit") as exit_process:
            await system._shutdown(context)
        exit_process.assert_called_once()
        self.assertIsNone(system._PENDING_ARGV)
        self.assertFalse(self.flag.exists())

    def test_reexec_consumes_pending_argv_once(self) -> None:
        system._PENDING_ARGV = [sys.executable, "-m", "fxbot"]
        with patch.object(system.os, "execv") as execv:
            system.reexec_if_pending()
            system.reexec_if_pending()
        execv.assert_called_once_with(sys.executable, [sys.executable, "-m", "fxbot"])
        self.assertIsNone(system._PENDING_ARGV)

    async def test_notifier_sends_once_after_bot_reconnects(self) -> None:
        self.flag.write_text(
            json.dumps(
                {
                    "target": "onebot11:100:group:300",
                    "platform": "onebot11",
                    "bot_id": "100",
                    "message": "FxBot 重启完成",
                    "created_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bot = SimpleNamespace(platform="onebot11", bot_id="100", send=AsyncMock())
        application = SimpleNamespace(bots=(bot,))
        await system._notify_after_restart(application)
        bot.send.assert_awaited_once_with(
            SessionTarget("onebot11", "100", "group", "300"), Message.text("FxBot 重启完成")
        )
        self.assertFalse(self.flag.exists())

    async def test_stale_restart_flag_is_discarded_without_sending(self) -> None:
        self.flag.write_text(
            json.dumps(
                {
                    "target": "onebot11:100:group:300",
                    "platform": "onebot11",
                    "bot_id": "100",
                    "message": "FxBot 重启完成",
                    "created_at": time.time() - system._FLAG_TTL_SECONDS - 1,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bot = SimpleNamespace(platform="onebot11", bot_id="100", send=AsyncMock())
        await system._notify_after_restart(SimpleNamespace(bots=(bot,)))
        bot.send.assert_not_awaited()
        self.assertFalse(self.flag.exists())

    async def test_docker_without_repo_mount_reports_setup(self) -> None:
        context = self.context()
        with (
            patch.object(system, "_docker_deployment", return_value=True),
            patch.object(system, "_docker_repo", return_value=None),
            patch.object(system, "_git_pull", AsyncMock()) as pull,
            patch.object(system, "_request_exit") as exit_process,
            patch.object(system, "_set_pending_restart") as pending,
        ):
            await system._update(context)
        pull.assert_not_awaited()
        exit_process.assert_not_called()
        pending.assert_not_called()
        text = context.bot.send.await_args.args[1].plain_text()
        self.assertIn("./:/repo", text)

    async def test_docker_with_mounted_repo_syncs_and_restarts(self) -> None:
        context = self.context()
        with (
            patch.object(system, "_docker_deployment", return_value=True),
            patch.object(system, "_docker_repo", return_value=Path("/repo")),
            patch.object(system, "_deps_changed", return_value=True),
            patch.object(system, "_git_pull", self.successful_pull("updated")),
            patch.object(system, "_sync_docker_dependencies", AsyncMock(return_value=(True, ""))) as sync,
            patch.object(system, "_send_report", AsyncMock()),
            patch.object(system, "_request_exit") as exit_process,
            patch.object(system, "_set_pending_restart") as pending,
        ):
            await system._update(context)
        sync.assert_awaited_once()
        exit_process.assert_called_once()
        pending.assert_called_once()

    async def test_docker_retries_failed_dependencies_when_git_is_up_to_date(self) -> None:
        outputs = iter(("updated", "Already up to date."))

        async def pull(on_success):
            on_success()
            return True, next(outputs), []

        with (
            patch.object(system, "_docker_deployment", return_value=True),
            patch.object(system, "_docker_repo", return_value=Path("/repo")),
            patch.object(system, "_deps_changed", return_value=True),
            patch.object(system, "_git_pull", AsyncMock(side_effect=pull)),
            patch.object(
                system, "_sync_docker_dependencies", AsyncMock(side_effect=[(False, "install failed"), (True, "")])
            ) as sync,
            patch.object(system, "_send_report", AsyncMock()) as report,
            patch.object(system, "_request_exit") as exit_process,
        ):
            await system._update(self.context())
            self.assertIn("依赖同步失败", report.await_args.args[1][0])
            self.assertIn("#更新", report.await_args.args[1][1])
            self.assertIsNone(system._PENDING_ARGV)
            self.assertFalse(self.flag.exists())
            exit_process.assert_not_called()
            await system._update(self.context())
            self.assertEqual(sync.await_count, 2)
            self.assertIn("没有新提交", report.await_args.args[1][0])
            self.assertIsNotNone(system._PENDING_ARGV)
            self.assertTrue(self.flag.exists())
            exit_process.assert_called_once()

    async def test_docker_does_not_prepare_restart_before_dependencies_succeed(self) -> None:
        async def sync():
            self.assertIsNone(system._PENDING_ARGV)
            self.assertFalse(self.flag.exists())
            return True, ""

        async def report(context, lines):
            self.assertIsNotNone(system._PENDING_ARGV)
            self.assertTrue(self.flag.exists())

        with (
            patch.object(system, "_docker_deployment", return_value=True),
            patch.object(system, "_docker_repo", return_value=Path("/repo")),
            patch.object(system, "_deps_changed", return_value=True),
            patch.object(system, "_git_pull", self.successful_pull()),
            patch.object(system, "_sync_docker_dependencies", AsyncMock(side_effect=sync)),
            patch.object(system, "_send_report", report),
            patch.object(system, "_request_exit") as exit_process,
        ):
            await system._update(self.context())
            exit_process.assert_called_once()

    async def test_docker_dependency_cancellation_does_not_restart(self) -> None:
        with (
            patch.object(system, "_docker_deployment", return_value=True),
            patch.object(system, "_docker_repo", return_value=Path("/repo")),
            patch.object(system, "_deps_changed", return_value=True),
            patch.object(system, "_git_pull", self.successful_pull()),
            patch.object(system, "_sync_docker_dependencies", AsyncMock(side_effect=asyncio.CancelledError())),
            patch.object(system, "_send_report", AsyncMock()) as report,
            patch.object(system, "_request_exit") as exit_process,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await system._update(self.context())
            self.assertIsNone(system._PENDING_ARGV)
            self.assertFalse(self.flag.exists())
            report.assert_not_awaited()
            exit_process.assert_not_called()

    async def test_docker_report_failure_after_sync_still_restarts(self) -> None:
        with (
            patch.object(system, "_docker_deployment", return_value=True),
            patch.object(system, "_docker_repo", return_value=Path("/repo")),
            patch.object(system, "_deps_changed", return_value=True),
            patch.object(system, "_git_pull", self.successful_pull()),
            patch.object(system, "_sync_docker_dependencies", AsyncMock(return_value=(True, ""))),
            patch.object(system, "_send_report", AsyncMock(side_effect=TimeoutError())),
            patch.object(system, "_request_exit") as exit_process,
        ):
            await system._update(self.context())
            self.assertIsNotNone(system._PENDING_ARGV)
            exit_process.assert_called_once()

    async def test_update_failure_reports_without_restarting(self) -> None:
        context = self.context()
        with (
            patch.object(system, "_git_pull", AsyncMock(return_value=(False, "fatal", []))),
            patch.object(system, "_send_report", AsyncMock()) as report,
            patch.object(system, "_request_exit") as exit_process,
            patch.object(system, "_set_pending_restart") as pending,
        ):
            await system._update(context)
        self.assertIn("更新失败", report.await_args.args[1][0])
        exit_process.assert_not_called()
        pending.assert_not_called()

    async def test_changed_dependencies_sync_before_restart(self) -> None:
        with (
            patch.object(system, "_deps_changed", return_value=True),
            patch.object(system, "_git_pull", self.successful_pull()),
            patch.object(system, "_sync_project_dependencies", AsyncMock(return_value=(True, "synced"))) as sync,
            patch.object(system, "_write_deps_stamp") as stamp,
            patch.object(system, "_send_report", AsyncMock()),
            patch.object(system, "_request_exit") as exit_process,
        ):
            await system._update(self.context())
        sync.assert_awaited_once()
        stamp.assert_called_once()
        exit_process.assert_called_once()
        self.assertIsNotNone(system._PENDING_ARGV)

    async def test_dependency_failure_does_not_restart(self) -> None:
        with (
            patch.object(system, "_deps_changed", return_value=True),
            patch.object(system, "_git_pull", self.successful_pull()),
            patch.object(system, "_sync_project_dependencies", AsyncMock(return_value=(False, "uv failed"))),
            patch.object(system, "_write_deps_stamp") as stamp,
            patch.object(system, "_send_report", AsyncMock()) as report,
            patch.object(system, "_request_exit") as exit_process,
        ):
            await system._update(self.context())
        stamp.assert_not_called()
        exit_process.assert_not_called()
        self.assertIsNone(system._PENDING_ARGV)
        self.assertIn("依赖同步失败", report.await_args.args[1][0])

    async def test_update_success_reports_and_restarts(self) -> None:
        context = self.context()
        with (
            patch.object(system, "_git_pull", self.successful_pull("Already up to date.")),
            patch.object(system, "_send_report", AsyncMock()) as report,
            patch.object(system, "_request_exit") as exit_process,
            patch.object(system, "_set_pending_restart") as pending,
        ):
            await system._update(context)
        self.assertIn("没有新提交", report.await_args.args[1][0])
        exit_process.assert_called_once()
        pending.assert_called_once()


class DependencyFingerprintTests(unittest.TestCase):
    """依赖指纹只在文件变化后要求同步。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        for name in ("pyproject.toml", "uv.lock"):
            (self.repo / name).write_text(f"# {name}\n", encoding="utf-8")
        self.enterContext(patch.object(system, "_DEPS_STAMP_FILE", root / "deps.sha256"))

    def test_missing_stamp_requires_sync(self) -> None:
        self.assertTrue(system._deps_changed(self.repo))

    def test_successful_stamp_skips_until_files_change(self) -> None:
        system._write_deps_stamp(self.repo)
        self.assertFalse(system._deps_changed(self.repo))
        (self.repo / "uv.lock").write_text("# changed\n", encoding="utf-8")
        self.assertTrue(system._deps_changed(self.repo))

    def test_missing_dependency_file_forces_sync(self) -> None:
        system._write_deps_stamp(self.repo)
        self.assertFalse(system._deps_changed(self.repo))
        (self.repo / "uv.lock").unlink()
        self.assertTrue(system._deps_changed(self.repo))


class DockerDependencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.repo, self.app = root / "repo", root / "app"
        self.repo.mkdir()
        self.app.mkdir()
        for name in ("pyproject.toml", "uv.lock"):
            (self.repo / name).write_text(f"# {name}\n", encoding="utf-8")
        self.enterContext(patch.object(system, "_docker_repo", return_value=self.repo))
        self.enterContext(patch.object(system, "_DOCKER_APP", self.app))

    async def test_sync_preserves_extra_packages_and_copies_required_files(self) -> None:
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b"synced", b""))
        with patch.object(system.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            self.assertEqual(await system._sync_docker_dependencies(), (True, "synced"))
        self.assertEqual(spawn.call_args.args, ("uv", "sync", "--frozen", "--no-dev", "--inexact"))
        self.assertEqual(spawn.call_args.kwargs["cwd"], str(self.app))
        self.assertEqual(spawn.call_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["start_new_session"], sys.platform != "win32")
        for name in ("pyproject.toml", "uv.lock"):
            self.assertEqual((self.repo / name).read_bytes(), (self.app / name).read_bytes())

    async def test_missing_required_file_is_reported_without_running_uv(self) -> None:
        (self.repo / "uv.lock").unlink()
        with patch.object(system.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            ok, detail = await system._sync_docker_dependencies()
        self.assertFalse(ok)
        self.assertTrue(detail)
        spawn.assert_not_awaited()

    async def test_missing_uv_is_reported(self) -> None:
        with patch.object(
            system.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("uv missing"))
        ):
            self.assertEqual(await system._sync_docker_dependencies(), (False, "uv missing"))

    async def test_nonzero_exit_is_reported(self) -> None:
        process = Mock(returncode=1)
        process.communicate = AsyncMock(return_value=(b"", b"install failed"))
        with patch.object(system.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            self.assertEqual(await system._sync_docker_dependencies(), (False, "install failed"))

    async def test_timeout_and_cancel_reclaim_installation_process(self) -> None:
        for cancel in (False, True):
            entered, released, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
            process = Mock(pid=123, returncode=None)

            async def communicate():
                entered.set()
                await released.wait()
                drained.set()
                return b"", b""

            process.communicate = AsyncMock(side_effect=communicate)

            def terminate(child):
                child.returncode = -1
                released.set()

            with (
                patch.object(system.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)),
                patch.object(system, "terminate_process", side_effect=terminate) as kill,
                patch.object(system, "_UPDATE_TIMEOUT_SECONDS", 0.02),
            ):
                task = asyncio.create_task(system._sync_docker_dependencies())
                await asyncio.wait_for(entered.wait(), 1)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertEqual(await task, (False, "依赖同步超时"))
                kill.assert_called_once_with(process)
                self.assertTrue(drained.is_set())
                self.assertEqual(process.communicate.await_count, 1)


class CommandProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_process_is_reaped_on_timeout_and_cancel(self) -> None:
        spawn = asyncio.create_subprocess_exec
        for cancel in (False, True):
            started = asyncio.Event()
            children = []

            async def launch(*args, **kwargs):
                child = await spawn(*args, **kwargs)
                children.append(child)
                started.set()
                return child

            try:
                with patch.object(system.asyncio, "create_subprocess_exec", side_effect=launch):
                    task = asyncio.create_task(
                        system._run_command(
                            sys.executable,
                            "-c",
                            "import time; time.sleep(30)",
                            cwd=Path.cwd(),
                            timeout=0.2,
                        )
                    )
                    await asyncio.wait_for(started.wait(), 5)
                    if cancel:
                        task.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                        await task
                    self.assertIsNotNone(children[0].returncode)
            finally:
                for child in children:
                    if child.returncode is None:
                        processes.terminate_process(child)
                        await asyncio.wait_for(child.wait(), 5)


class ProcessCleanupTests(unittest.TestCase):
    def test_windows_terminates_descendants_and_owned_parent_without_running_commands(self):
        parent, root, child = Mock(pid=123, returncode=None), Mock(), Mock()
        root.children.return_value = [child]
        with (
            patch.object(processes.sys, "platform", "win32"),
            patch.object(processes.psutil, "Process", return_value=root),
        ):
            processes.terminate_process(parent)
        root.children.assert_called_once_with(recursive=True)
        child.kill.assert_called_once()
        parent.kill.assert_called_once()

    def test_exited_process_is_not_looked_up_again_by_pid(self):
        with patch.object(processes.psutil, "Process") as lookup:
            processes.terminate_process(Mock(returncode=0))
        lookup.assert_not_called()


class _PermissionStoreStub:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.saves = 0

    def load(self) -> dict:
        return self.data

    def save(self, data: dict) -> None:
        # 保持测试里持有的引用同步，模拟真实存储的落盘后读取。
        self.data.clear()
        self.data.update(data)
        self.saves += 1


def command_event(text: str, *, mentions=(), reply_user=None, bot_id="100", user="200") -> Event:
    segments = [MessageSegment.mention(item) for item in mentions]
    segments.append(MessageSegment.text(text))
    return Event(
        event_id="event",
        message_id="message",
        event_type="message",
        platform="onebot11",
        bot_id=bot_id,
        user_id=user,
        user_name="超管",
        target=SessionTarget("onebot11", bot_id, "group", "300"),
        message=Message(segments),
        reply=ReplyTarget("quoted", reply_user) if reply_user else None,
    )


class CommandBlacklistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.application = Application()
        self.application.command_metadata.update(
            {
                "entertain": {"today": "今日运势", "draw": "抽签"},
                "chat": {"context": "记录群聊上下文"},
            }
        )
        self.application.plugin_metadata["entertain"] = SimpleNamespace(name="娱乐")
        self.data = {
            "top": perm_entry_default(),
            "sub_plugins": {
                "entertain": {
                    "top": perm_entry_default(),
                    "commands": {
                        "today": perm_entry_default(),
                        "draw": perm_entry_default(),
                    },
                }
            },
        }
        self.store = _PermissionStoreStub(self.data)
        self.enterContext(patch.object(system, "get_storage", return_value=self.store))
        self.bot = SimpleNamespace(platform="onebot11", bot_id="100", send=AsyncMock(), capabilities=())

    def context(self, event: Event, argument: str = "") -> HandlerContext:
        return HandlerContext(self.bot, event, MatchResult(argument, (argument,)))

    def blacklisted(self, command: str = "today") -> list[str]:
        return self.data["sub_plugins"]["entertain"]["commands"][command]["blacklist"]["users"]

    async def moderate(self, event: Event, argument: str, *, remove: bool = False) -> str:
        await system._moderate_command(self.application, self.context(event, argument), remove=remove)
        return self.bot.send.await_args.args[1].plain_text()

    async def test_mention_target_can_appear_before_or_after_the_command(self) -> None:
        reply = await self.moderate(command_event("拉黑 今日运势", mentions=("777",)), "今日运势")
        self.assertIn("已对", reply)
        self.assertEqual(self.blacklisted(), ["777"])
        self.assertEqual(self.store.saves, 1)

    async def test_qq_number_and_reply_target_are_accepted(self) -> None:
        event = command_event("拉黑 今日运势 123456")
        await self.moderate(event, "今日运势 123456")
        self.assertEqual(self.blacklisted(), ["123456"])
        event = command_event("拉黑 今日运势", reply_user="888")
        await self.moderate(event, "今日运势")
        self.assertEqual(self.blacklisted(), ["123456", "888"])

    async def test_bot_mention_is_not_a_target(self) -> None:
        event = command_event("拉黑 今日运势", mentions=("100", "777"))
        await self.moderate(event, "今日运势")
        self.assertEqual(self.blacklisted(), ["777"])

    async def test_unblacklist_only_touches_the_named_command(self) -> None:
        self.blacklisted("today").extend(["777", "888"])
        self.blacklisted("draw").append("777")
        event = command_event("拉白 今日运势", mentions=("777",))
        reply = await self.moderate(event, "今日运势", remove=True)
        self.assertIn("解除拉黑", reply)
        self.assertEqual(self.blacklisted("today"), ["888"])
        self.assertEqual(self.blacklisted("draw"), ["777"])

    async def test_internal_prefixed_and_display_names_all_resolve(self) -> None:
        for spec in ("today", "entertain:today", "娱乐:today", "娱乐:今日运势", "#今日运势"):
            self.assertEqual(system._resolve_command(self.application, spec), ("entertain", "today"))

    async def test_blacklist_is_enforced_by_feature_policy(self) -> None:
        await self.moderate(command_event("拉黑 今日运势", mentions=("777",)), "今日运势")
        policy = FeaturePolicy()

        def context(user: str) -> PermContext:
            return PermContext(
                user_id=SessionTarget("onebot11", "100", "private", user).key(),
                group_id=None,
                user_level=PermLevel.MEMBER,
                is_group=False,
                is_private=True,
            )

        self.assertFalse(await policy.evaluate(self.data, "entertain:today", context("777")))
        self.assertTrue(await policy.evaluate(self.data, "entertain:today", context("999")))
        self.assertTrue(await policy.evaluate(self.data, "entertain:draw", context("777")))

    async def test_unknown_whole_plugin_and_core_commands_are_rejected(self) -> None:
        reply = await self.moderate(command_event("拉黑 不存在", mentions=("777",)), "不存在")
        self.assertIn("没有找到命令", reply)
        reply = await self.moderate(command_event("拉黑 娱乐", mentions=("777",)), "娱乐")
        self.assertIn("是插件", reply)
        reply = await self.moderate(command_event("拉黑 记录群聊上下文", mentions=("777",)), "记录群聊上下文")
        self.assertIn("不经过权限配置", reply)
        self.assertEqual(self.store.saves, 0)

    async def test_ambiguous_display_name_lists_candidates(self) -> None:
        self.application.command_metadata["useful"] = {"today": "今日运势"}
        with self.assertRaises(system._CommandLookupError) as caught:
            system._resolve_command(self.application, "今日运势")
        self.assertIn("多个命令", str(caught.exception))
        self.assertIn("entertain:today", str(caught.exception))
        self.assertIn("useful:today", str(caught.exception))

    async def test_missing_target_reports_how_to_supply_one(self) -> None:
        reply = await self.moderate(command_event("拉黑 今日运势"), "今日运势")
        self.assertIn("请 @ 目标用户", reply)
        self.assertEqual(self.store.saves, 0)

    async def test_blacklist_command_allows_tight_spelling_without_swallowing_kick(self) -> None:
        matcher = regex_match(system._BLACKLIST_PATTERN)
        for text in ("#拉黑今日运势", "#拉黑 今日运势"):
            self.assertEqual(matcher(command_event(text)).group(1), "今日运势")
        # @ 段不进入纯文本，@ 在命令前面时紧贴也能解析出命令名。
        self.assertEqual(matcher(command_event("#拉黑今日运势", mentions=("777",))).group(1), "今日运势")
        self.assertIsNone(matcher(command_event("#拉黑踢 @777")))
        self.assertIsNone(matcher(command_event("#拉黑踢出")))
        self.assertEqual(
            regex_match(system._UNBLACKLIST_PATTERN)(command_event("#拉白今日运势")).group(1), "今日运势"
        )


if __name__ == "__main__":
    unittest.main()
