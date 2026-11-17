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

from fxbot.core import Message, SessionTarget, HandlerContext
from fxbot.runtime import processes, system_commands as system
from fxbot.core.matching import MatchResult


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
        self.assertEqual(data["message"], "✅ FxBot 重启成功")

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
                    "message": "✅ FxBot 重启成功",
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
            SessionTarget("onebot11", "100", "group", "300"), Message.text("✅ FxBot 重启成功")
        )
        self.assertFalse(self.flag.exists())

    async def test_stale_restart_flag_is_discarded_without_sending(self) -> None:
        self.flag.write_text(
            json.dumps(
                {
                    "target": "onebot11:100:group:300",
                    "platform": "onebot11",
                    "bot_id": "100",
                    "message": "✅ FxBot 重启成功",
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

    async def test_update_success_reports_and_restarts(self) -> None:
        context = self.context()
        with (
            patch.object(system, "_git_pull", self.successful_pull("Already up to date.")),
            patch.object(system, "_send_report", AsyncMock()) as report,
            patch.object(system, "_request_exit") as exit_process,
            patch.object(system, "_set_pending_restart") as pending,
        ):
            await system._update(context)
        self.assertIn("无更新", report.await_args.args[1][0])
        exit_process.assert_called_once()
        pending.assert_called_once()


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


if __name__ == "__main__":
    unittest.main()
