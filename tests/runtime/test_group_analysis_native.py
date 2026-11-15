"""原生群日报的配置、身份隔离、命令与定时准入。"""

from __future__ import annotations

import json
import time
import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

from fastapi.testclient import TestClient

from fxbot.core import SessionTarget, HistoryMessage
from fxbot.config import SYSTEM_DEFAULTS, ConfigManager, get_manager
from fxbot.core.plugins import PluginContext, PluginDefinition, load_plugins
from fxbot.core.scheduler import Scheduler
from fxbot.runtime.server import create_app
from fxbot.core.application import Application
from fxbot.permission.types import perm_entry_default
from fxbot.runtime.settings import Settings
from fxbot.permission.storage import PermissionStorage
from fxbot.platforms.onebot11 import OneBot11Client, parse_event
from tests.runtime.test_server import HEADERS
from fxbot.runtime.notifications import NotificationService
from fxbot.plugins.group_analysis import config, report
from tests.migration.test_onebot11 import payload
from fxbot.permission.authorization import Authorizer
from fxbot.platforms.onebot11.napcat import NapCatClient
from fxbot.plugins.group_analysis.commands import COMMAND_NAME
from fxbot.plugins.group_analysis.scheduler import JOB_NAME, ReportSchedule
from fxbot.plugins.group_analysis.analyzers.runner import empty_analysis

FIRST = SessionTarget("onebot11", "100", "group", "300")
SECOND = SessionTarget("onebot11", "101", "group", "300")


class ReportConfigTests(unittest.TestCase):
    """配置不能猜测平台归属，也不能把错误值强制转换成有效计划。"""

    def test_full_scoped_targets_are_required(self) -> None:
        for group, key in (("basic", "groups"), ("auto", "auto_groups")):
            for value in ("300", ["300"], ["onebot11:100:private:200"], [True]):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    config.validate_config({group: {key: value}})
            config.validate_config({group: {key: [FIRST.key()]}})

    def test_invalid_schedule_and_limits_are_rejected(self) -> None:
        for item in (
            {"auto": {"auto_hour": 24}},
            {"auto": {"auto_minute": True}},
            {"basic": {"max_messages": 5001}},
            {"basic": {"days": "3"}},
            {"analysis_features": {"max_topics": 0}},
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                config.validate_config(item)

    def test_napcat_selection_is_explicit_and_tied_to_configured_bot(self) -> None:
        settings = Settings("token", {"onebot11": frozenset({"100"})}, onebot11_implementations={"100": "napcat"})
        self.assertEqual(settings.onebot11_implementations["100"], "napcat")
        for mapping in ({"101": "napcat"}, {"100": "unknown"}, {"100": True}, {"100": []}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                Settings("token", {"onebot11": frozenset({"100"})}, onebot11_implementations=mapping)


class ReportGenerationTests(unittest.IsolatedAsyncioTestCase):
    """按完整会话去重并持久化，不接受跨连接数据。"""

    async def asyncSetUp(self) -> None:
        self.settings = deepcopy(config.DEFAULTS)
        self.settings["basic"]["min_messages"] = 1
        self.enterContext(patch.object(config, "get_config", side_effect=lambda: deepcopy(self.settings)))
        self.enterContext(patch.object(report, "get_config", side_effect=lambda: deepcopy(self.settings)))
        self.enterContext(patch.object(report, "resolve_persona", return_value=""))
        self.analyze = self.enterContext(
            patch.object(report, "analyze", AsyncMock(side_effect=lambda *a, **k: empty_analysis()))
        )
        self.render = self.enterContext(patch.object(report, "render_report", AsyncMock(return_value=b"PNG")))
        self.saved = self.enterContext(patch.object(report.GroupAnalysisReport, "upsert", AsyncMock()))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.object(report, "data_dir", return_value=self.root))
        self.bot = NapCatClient("100", AsyncMock())
        self.history = self.enterContext(
            patch.object(
                self.bot,
                "fetch_group_history",
                AsyncMock(
                    return_value=[
                        HistoryMessage("1", 100000, "200", "成员", "正文"),
                    ]
                ),
            )
        )

    async def generate(self, bot=None, target=FIRST, **kwargs):
        return await report.generate(
            bot or self.bot, target, days=1, end=110000, label="测试", report_date="2026-11-13", **kwargs
        )

    async def test_record_and_file_use_scoped_target_and_default_persona_session(self) -> None:
        result = await self.generate()
        self.assertTrue(result.ok)
        saved = self.saved.await_args.kwargs
        self.assertEqual(saved["group_id"], FIRST.key())
        self.assertEqual(Path(saved["image_path"]).read_bytes(), b"PNG")
        self.assertTrue(Path(saved["image_path"]).is_relative_to(self.root))
        self.history.assert_awaited_once_with(FIRST, start_time=23600, end_time=110000, limit=1000)
        report.resolve_persona.assert_called_once_with(FIRST.key())

    async def test_same_group_id_different_bots_do_not_share_files_or_running_lock(self) -> None:
        await self.generate()
        other = NapCatClient("101", AsyncMock())
        with patch.object(
            other, "fetch_group_history", AsyncMock(return_value=[HistoryMessage("2", 100000, "200", "成员", "正文")])
        ):
            self.assertTrue((await self.generate(other, SECOND)).ok)
        first, second = [call.kwargs for call in self.saved.await_args_list]
        self.assertNotEqual(first["group_id"], second["group_id"])
        self.assertNotEqual(first["image_path"], second["image_path"])

    async def test_no_history_capability_returns_unsupported_without_query(self) -> None:
        bot = OneBot11Client("100", AsyncMock())
        self.assertEqual((await self.generate(bot)).reason, "unsupported")
        self.analyze.assert_not_awaited()
        bot._sender.assert_not_awaited()

    async def test_internal_error_propagates_and_releases_running_marker(self) -> None:
        self.history.side_effect = RuntimeError("implementation bug")
        with self.assertRaisesRegex(RuntimeError, "implementation bug"):
            await self.generate()
        self.assertFalse(report.is_running(FIRST.key()))

    async def test_cancelled_generation_releases_running_marker(self) -> None:
        entered = asyncio.Event()

        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.history.side_effect = waiting
        task = asyncio.create_task(self.generate())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(report.is_running(FIRST.key()))
        self.assertEqual((await self.generate()).reason, "busy")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(report.is_running(FIRST.key()))

    async def test_wrong_connection_rejected_before_history_or_model(self) -> None:
        with self.assertRaises(ValueError):
            await self.generate(target=SECOND)
        self.history.assert_not_awaited()

    async def test_group_allowlist_does_not_grant_same_id_to_other_bot(self) -> None:
        self.settings["basic"]["groups"] = [SECOND.key()]
        self.assertEqual((await self.generate()).reason, "group_not_allowed")
        self.history.assert_not_awaited()


class ReportPluginTests(unittest.IsolatedAsyncioTestCase):
    """真实路由权限和调度定义，生成/渲染使用可控替身。"""

    async def asyncSetUp(self) -> None:
        self.app = Application()
        self.scheduler = Scheduler(self.app)
        self.configuration = Mock(spec=ConfigManager)
        self.configuration.get_system.return_value = deepcopy(SYSTEM_DEFAULTS)
        self.unsubscribe = Mock()
        self.configuration.subscribe.return_value = self.unsubscribe
        self.storage = Mock(spec=PermissionStorage)
        self.permissions = {"top": perm_entry_default(), "sub_plugins": {}}
        self.storage.load.return_value = self.permissions
        self.authorizer = Authorizer(self.configuration, self.storage)
        self.notifications = Mock(spec=NotificationService)
        self.notifications.allowed = AsyncMock(return_value=True)
        self.notifications.send = AsyncMock(return_value=True)
        self.settings = deepcopy(config.DEFAULTS)
        self.settings["auto"]["auto_groups"] = [FIRST.key()]
        self.enterContext(patch.object(config, "get_config", side_effect=lambda: deepcopy(self.settings)))
        self.enterContext(
            patch("fxbot.plugins.group_analysis.scheduler.get_config", side_effect=lambda: deepcopy(self.settings))
        )
        self.feature = ReportSchedule(self.app, self.notifications)
        with patch("fxbot.plugins.group_analysis.scheduler.ReportSchedule", return_value=self.feature):
            load_plugins(
                PluginContext(
                    self.app, self.scheduler, self.authorizer, self.notifications, self.authorizer.configuration
                ),
                (PluginDefinition("group_analysis", "fxbot.plugins.group_analysis"),),
            )
            await self.app.start()
        self.addAsyncCleanup(self.app.stop)
        self.bot = NapCatClient("100", AsyncMock(return_value={"message_id": 1}))
        self.app.register(self.bot, close=AsyncMock())
        self.name = self.enterContext(patch.object(self.bot, "target_name", AsyncMock(return_value="测试群")))

    async def dispatch(self):
        incoming = payload()
        incoming["message"] = [{"type": "text", "data": {"text": "/群聊分析 3"}}]
        return await self.app.router.dispatch(self.bot, parse_event(incoming, bot_id="100"))

    async def test_manual_command_sends_core_image_to_actual_group(self) -> None:
        with patch(
            "fxbot.plugins.group_analysis.commands.generate", AsyncMock(return_value=report.ReportResult(True, b"PNG"))
        ) as generate:
            result = await self.dispatch()
        self.assertEqual(result.failed, ())
        self.assertTrue(result.blocked)
        self.assertEqual(generate.await_args.args[1], FIRST)
        self.assertEqual(generate.await_args.kwargs["days"], 3)
        self.assertEqual(generate.await_args.kwargs["session_id"], FIRST.key())
        sent = self.bot._sender.await_args.args[1]
        self.assertEqual(sent["group_id"], 300)
        self.assertEqual(sent["message"][0]["type"], "image")

    async def test_permission_revocation_during_generation_prevents_image_delivery(self) -> None:
        async def generate(*args, **kwargs):
            self.permissions["sub_plugins"]["group_analysis"]["commands"][COMMAND_NAME]["enabled"] = False
            return report.ReportResult(True, b"PNG")

        with patch("fxbot.plugins.group_analysis.commands.generate", AsyncMock(side_effect=generate)):
            await self.dispatch()
        self.assertEqual(self.bot._sender.await_count, 1)
        self.assertEqual(self.bot._sender.await_args.args[1]["message"][0]["type"], "text")

    async def test_schedule_save_reconfigures_and_stop_unsubscribes(self) -> None:
        job = next(job for job in self.scheduler.jobs if job.name == JOB_NAME)
        self.assertEqual(job.schedule.hours, (8,))
        self.settings["auto"]["auto_hour"] = 10
        self.configuration.subscribe.call_args.args[0]()
        job = next(job for job in self.scheduler.jobs if job.name == JOB_NAME)
        self.assertEqual(job.schedule.hours, (10,))
        await self.app.stop()
        self.unsubscribe.assert_called_once()
        self.assertNotIn(JOB_NAME, [job.name for job in self.scheduler.jobs])

    async def test_background_denied_target_skips_expensive_generation(self) -> None:
        self.notifications.allowed.return_value = False
        with patch("fxbot.plugins.group_analysis.scheduler.generate", AsyncMock()) as generate:
            await self.feature.run()
        generate.assert_not_awaited()
        self.notifications.send.assert_not_awaited()

    async def test_background_uses_exact_target_and_passes_revalidation(self) -> None:
        with patch(
            "fxbot.plugins.group_analysis.scheduler.generate", AsyncMock(return_value=report.ReportResult(True, b"PNG"))
        ) as generate:
            await self.feature.run()
        self.assertEqual(generate.await_args.args[:2], (self.bot, FIRST))
        send = self.notifications.send.await_args
        self.assertEqual(send.args[1], FIRST)
        self.assertTrue(send.kwargs["valid"]())
        self.settings["auto"]["auto_groups"] = []
        self.assertFalse(send.kwargs["valid"]())

    async def test_offline_exact_target_does_not_fall_back_to_another_bot(self) -> None:
        self.app.unregister(self.bot)
        other = NapCatClient("101", AsyncMock())
        self.app.register(other, close=AsyncMock())
        with patch("fxbot.plugins.group_analysis.scheduler.generate", AsyncMock()) as generate:
            await self.feature.run()
        generate.assert_not_awaited()
        other._sender.assert_not_awaited()


class StandaloneReportTests(unittest.TestCase):
    """原始协议查询到数据库、图片发送的完整闭环，不连接真实 QQ 或模型。"""

    def test_napcat_command_fetches_scoped_history_persists_and_sends_report(self) -> None:
        system = deepcopy(SYSTEM_DEFAULTS)
        system["chat"]["enabled"] = False
        settings = deepcopy(config.DEFAULTS)
        settings["basic"]["min_messages"] = 1
        for key in (
            "topic_analysis_enabled",
            "user_title_analysis_enabled",
            "golden_quote_analysis_enabled",
            "chat_quality_analysis_enabled",
        ):
            settings["analysis_features"][key] = False
        with (
            patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(system)),
            patch.object(config.REG, "load", side_effect=lambda: deepcopy(settings)),
            patch.object(report, "render_report", AsyncMock(return_value=b"PNG-report")) as render,
        ):
            app = create_app(
                Settings("test-only", {"onebot11": frozenset({"100"})}, onebot11_implementations={"100": "napcat"})
            )
            with TestClient(app) as client:
                with client.websocket_connect("/onebot/v11/ws", headers=dict(HEADERS)) as websocket:
                    incoming = payload()
                    incoming["message"] = [{"type": "text", "data": {"text": "/群聊分析"}}]
                    websocket.send_json(incoming)
                    seen = []
                    while True:
                        action = websocket.receive_json()
                        seen.append(action["action"])
                        data = {}
                        if action["action"] == "get_group_info":
                            data = {"group_id": 300, "group_name": "测试群"}
                        elif action["action"] == "get_group_msg_history":
                            self.assertEqual(action["params"]["group_id"], "300")
                            self.assertNotIn("reverseOrder", action["params"])
                            timestamp = int(time.time())
                            message = {
                                "message_id": 11,
                                "message_seq": 110,
                                "group_id": 300,
                                "user_id": 200,
                                "sender": {"nickname": "成员"},
                                "message_type": "group",
                                "time": timestamp - 100,
                                "message": [{"type": "text", "data": {"text": "真实历史"}}],
                            }
                            oldest = {**message, "message_id": 10, "message_seq": 100, "time": timestamp - 90000}
                            data = {"messages": [message, oldest]}
                        elif action["action"] == "send_group_msg":
                            self.assertEqual(action["params"]["group_id"], 300)
                            data = {"message_id": 99}
                        else:
                            self.fail(f"不应调用的协议动作: {action['action']}")
                        websocket.send_json({"echo": action["echo"], "status": "ok", "retcode": 0, "data": data})
                        if action["action"] == "send_group_msg" and action["params"]["message"][0]["type"] == "image":
                            self.assertEqual(
                                action["params"]["message"][0]["data"]["file"], "base64://UE5HLXJlcG9ydA=="
                            )
                            break
                    self.assertEqual(
                        seen, ["send_group_msg", "get_group_info", "get_group_msg_history", "send_group_msg"]
                    )
                    self.assertEqual(render.await_args.args[0]["stats"]["total"], 1)

                    async def saved():
                        return await report.GroupAnalysisReport.select_rows(group_id=FIRST.key())

                    rows = client.portal.call(saved)
                    self.assertTrue(rows)
                    self.assertEqual(rows[-1].message_count, 1)
                    self.assertEqual(Path(rows[-1].image_path).read_bytes(), b"PNG-report")
                    self.assertEqual(json.loads(rows[-1].summary)["topics"], [])

    def test_standard_v11_reports_unsupported_without_guessing_history_api(self) -> None:
        app = create_app(Settings("test-only", {"onebot11": frozenset({"100"})}))
        with TestClient(app) as client:
            with client.websocket_connect("/onebot/v11/ws", headers=dict(HEADERS)) as websocket:
                incoming = payload()
                incoming["message"] = [{"type": "text", "data": {"text": "/群聊分析"}}]
                websocket.send_json(incoming)
                action = websocket.receive_json()
                self.assertEqual(action["action"], "send_group_msg")
                self.assertIn("不支持拉取群历史", action["params"]["message"][0]["data"]["text"])
                websocket.send_json({"echo": action["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 99}})
