"""直播插件原生路由、状态持久化与逐目标通知回归。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock, AsyncMock, patch

from fastapi.testclient import TestClient

from fxbot.core import Message, SessionTarget
from fxbot.config import SYSTEM_DEFAULTS, ConfigManager, get_manager
from fxbot.plugins import bilibili_live
from fxbot.core.plugins import PluginContext, PluginDefinition, load_plugins
from fxbot.core.scheduler import Scheduler
from fxbot.runtime.server import create_app
from fxbot.core.application import Application
from fxbot.membership.guard import MembershipGuard
from fxbot.permission.types import perm_entry_default
from fxbot.runtime.settings import Settings
from fxbot.permission.helpers import upsert_command_defaults
from fxbot.permission.storage import PermissionStorage
from fxbot.platforms.onebot11 import OneBot11Client, parse_event
from tests.runtime.test_server import HEADERS
from fxbot.runtime.notifications import NotificationService
from tests.migration.test_onebot11 import payload
from fxbot.permission.authorization import Authorizer
from fxbot.plugins.bilibili_live.store import LiveStore
from fxbot.plugins.bilibili_live.client import LiveRoomSnapshot, BilibiliLiveError
from fxbot.plugins.bilibili_live.scheduler import LivePoller

ROOM = LiveRoomSnapshot(123, 456, "主播", "标题", "分区", "", 0, "")
FIRST = SessionTarget("onebot11", "100", "group", "300")
SECOND = SessionTarget("onebot11", "101", "group", "300")
ACTOR = SessionTarget("onebot11", "100", "private", "200").key()


class LiveStoreTests(unittest.TestCase):
    """坏文件和无连接身份的旧格式均不能被空状态覆盖。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.store = LiveStore(self.path)

    def test_subscription_scopes_include_bot_and_platform(self) -> None:
        self.assertTrue(self.store.add_room(FIRST, ROOM, ACTOR))
        self.assertFalse(self.store.add_room(FIRST, ROOM, ACTOR))
        actor = SessionTarget(SECOND.platform, SECOND.bot_id, "private", "200").key()
        self.assertTrue(self.store.add_room(SECOND, ROOM, actor))
        other = SessionTarget("onebot12.wechat", "100", "group", "300")
        self.store.add_room(other, ROOM, SessionTarget(other.platform, other.bot_id, "private", "200").key())
        self.assertEqual(len(self.store.subscriptions()), 3)
        self.assertEqual(len(self.store.rooms()), 1)
        self.assertTrue(self.store.remove_room(FIRST, 123))
        self.assertIsNotNone(self.store.subscription(SECOND))
        self.assertIsNotNone(self.store.subscription(other))
        self.assertIsNone(self.store.subscription(FIRST))

    def test_subscribing_to_live_room_does_not_request_an_initial_push(self) -> None:
        live = replace(ROOM, live_status=1)
        self.store.add_room(FIRST, live, ACTOR)
        state = self.store.room_state(123)
        self.assertTrue(state.is_live)
        self.assertIn(FIRST.key(), state.delivered)
        self.assertEqual(LiveStore(self.path).room_state(123), state)

    def test_stale_delivery_cannot_confirm_a_new_live_cycle(self) -> None:
        self.store.add_room(FIRST, ROOM, ACTOR)
        self.store.record_state(123, True, frozenset())
        generation = self.store.room_state(123).generation
        self.store.record_state(123, False, frozenset())
        self.store.record_state(123, True, frozenset())
        self.assertFalse(self.store.mark_delivered(123, FIRST, generation=generation))
        self.assertEqual(self.store.room_state(123).delivered, frozenset())

    def test_corrupt_or_legacy_file_is_preserved(self) -> None:
        for original in ("{bad", '{"subscriptions": [], "room_states": {}}'):
            with self.subTest(original=original):
                self.path.write_text(original, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.add_room(FIRST, ROOM, ACTOR)
                self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_failed_atomic_replace_preserves_previous_state(self) -> None:
        self.store.add_room(FIRST, ROOM, ACTOR)
        previous = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.remove_room(FIRST, 123)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_no_change_does_not_rewrite_the_state(self) -> None:
        self.store.add_room(FIRST, ROOM, ACTOR)
        with patch.object(self.store, "_write") as save:
            self.store.record_state(123, False, frozenset())
        save.assert_not_called()

    def test_operator_cannot_come_from_another_bot(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_room(SECOND, ROOM, ACTOR)
        self.assertFalse(self.path.exists())


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    """后台通知没有用户管理员豁免，也不会寻找替代连接。"""

    async def asyncSetUp(self) -> None:
        self.app = Application()
        await self.app.start()
        self.addAsyncCleanup(self.app.stop)
        self.settings = deepcopy(SYSTEM_DEFAULTS)
        self.config = Mock(spec=ConfigManager)
        self.config.get_system.side_effect = lambda: self.settings
        self.auth = Mock(spec=Authorizer)
        self.auth.check_target = AsyncMock(return_value=True)
        self.membership = Mock(spec=MembershipGuard)
        self.service = NotificationService(self.app, self.config, self.auth, self.membership)
        self.bot = OneBot11Client("100", AsyncMock(return_value={"message_id": 1}))
        self.app.register(self.bot, close=AsyncMock())

    async def test_exact_connection_only(self) -> None:
        self.assertFalse(await self.service.send("bilibili_live", SECOND, Message.text("消息"), valid=lambda: True))
        self.bot._sender.assert_not_awaited()
        self.assertTrue(await self.service.send("bilibili_live", FIRST, Message.text("消息"), valid=lambda: True))
        self.bot._sender.assert_awaited_once()

    async def test_expired_membership_and_disabled_plugin_prevent_push(self) -> None:
        self.settings["membership"]["enabled"] = True
        self.membership.check_membership_detail = AsyncMock(return_value=Mock(allowed=False))
        self.assertFalse(await self.service.send("bilibili_live", FIRST, Message.text("消息"), valid=lambda: True))
        self.membership.check_membership_detail.assert_awaited_once_with(FIRST.key(), None)
        self.bot._sender.assert_not_awaited()
        self.auth.check_target.return_value = False
        self.membership.check_membership_detail.reset_mock()
        self.assertFalse(await self.service.send("bilibili_live", FIRST, Message.text("消息"), valid=lambda: True))
        self.membership.check_membership_detail.assert_not_awaited()

    async def test_revoked_subscription_is_rechecked_after_membership_query(self) -> None:
        active = {"value": True}
        self.settings["membership"]["enabled"] = True

        async def check(*args):
            active["value"] = False
            return Mock(allowed=True)

        self.membership.check_membership_detail = AsyncMock(side_effect=check)
        self.assertFalse(
            await self.service.send("bilibili_live", FIRST, Message.text("消息"), valid=lambda: active["value"])
        )
        self.bot._sender.assert_not_awaited()

    async def test_known_send_failure_does_not_try_another_account(self) -> None:
        self.bot._sender.side_effect = TimeoutError()
        other = OneBot11Client("101", AsyncMock())
        self.app.register(other, close=AsyncMock())
        self.assertFalse(await self.service.send("bilibili_live", FIRST, Message.text("消息"), valid=lambda: True))
        other._sender.assert_not_awaited()

    async def test_internal_client_error_is_not_swallowed(self) -> None:
        self.bot._sender.side_effect = RuntimeError("implementation bug")
        with self.assertRaisesRegex(RuntimeError, "implementation bug"):
            await self.service.send("bilibili_live", FIRST, Message.text("消息"), valid=lambda: True)


class LivePollerTests(unittest.IsolatedAsyncioTestCase):
    """一次直播周期内逐目标确认，离线者恢复后单独补发。"""

    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = LiveStore(Path(directory.name) / "state.json")
        self.store.add_room(FIRST, ROOM, ACTOR)
        self.store.add_room(SECOND, ROOM, SessionTarget(SECOND.platform, SECOND.bot_id, "private", "200").key())
        self.notifications = Mock(spec=NotificationService)
        self.fetch = AsyncMock(return_value=ROOM)
        self.poller = LivePoller(self.store, self.notifications, self.fetch)
        await self.poller.calibrate()

    async def test_each_target_gets_one_delivery_even_when_another_is_offline(self) -> None:
        self.fetch.return_value = replace(ROOM, live_status=1)
        self.notifications.send = AsyncMock(side_effect=[True, False, True])
        self.assertEqual(await self.poller.check_and_push(), 1)
        self.assertEqual(self.store.room_state(123).delivered, frozenset({FIRST.key()}))
        self.assertEqual(await self.poller.check_and_push(), 1)
        self.assertEqual(await self.poller.check_and_push(), 0)
        self.assertEqual([call.args[1] for call in self.notifications.send.await_args_list], [FIRST, SECOND, SECOND])

    async def test_new_live_cycle_can_notify_again(self) -> None:
        self.notifications.send = AsyncMock(return_value=True)
        self.fetch.return_value = replace(ROOM, live_status=1)
        self.assertEqual(await self.poller.check_and_push(), 2)
        self.fetch.return_value = ROOM
        await self.poller.check_and_push()
        self.fetch.return_value = replace(ROOM, live_status=1)
        self.assertEqual(await self.poller.check_and_push(), 2)

    async def test_startup_calibration_does_not_push_already_live_rooms(self) -> None:
        self.fetch.return_value = replace(ROOM, live_status=1)
        await self.poller.calibrate()
        self.assertEqual(await self.poller.check_and_push(), 0)
        self.notifications.send.assert_not_called()

    async def test_query_failure_keeps_previous_state(self) -> None:
        self.fetch.side_effect = BilibiliLiveError("upstream unavailable")
        previous = self.store.path.read_bytes()
        self.assertEqual(await self.poller.check_and_push(), 0)
        self.assertEqual(self.store.path.read_bytes(), previous)

    async def test_internal_query_failure_waits_for_other_owned_queries_to_finish(self) -> None:
        self.store.add_room(FIRST, replace(ROOM, room_id=124), ACTOR)
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def query(room_id, **kwargs):
            if room_id == 123:
                raise RuntimeError("query implementation bug")
            entered.set()
            await release.wait()
            finished.set()
            return replace(ROOM, room_id=room_id)

        self.fetch.side_effect = query
        task = asyncio.create_task(self.poller.calibrate())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaisesRegex(RuntimeError, "query implementation bug"):
            await task
        self.assertTrue(finished.is_set())

    async def test_unsubscribe_during_send_does_not_recreate_subscription(self) -> None:
        self.fetch.return_value = replace(ROOM, live_status=1)

        async def send(feature, target, message, *, valid):
            self.store.remove_room(target, 123)
            self.assertFalse(valid())
            return False

        self.notifications.send = AsyncMock(side_effect=send)
        self.assertEqual(await self.poller.check_and_push(), 0)
        self.assertEqual(self.store.subscriptions(), ())


class LiveCommandTests(unittest.IsolatedAsyncioTestCase):
    """命令走真实原生路由与领域权限，不创建 Matcher 替身。"""

    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = LiveStore(Path(directory.name) / "state.json")
        self.app = Application()
        self.scheduler = Scheduler(self.app)
        self.config = Mock(spec=ConfigManager)
        self.config.get_system.return_value = deepcopy(SYSTEM_DEFAULTS)
        self.permissions = {"top": perm_entry_default(), "sub_plugins": {}}
        self.storage = Mock(spec=PermissionStorage)
        self.storage.load.return_value = self.permissions
        self.auth = Authorizer(self.config, self.storage)
        self.feature = bilibili_live.commands.LiveCommands(self.store)
        self.enterContext(patch.object(bilibili_live.commands.live, "store", self.store))
        self.enterContext(patch.object(bilibili_live.commands, "store", self.store))
        with patch.object(bilibili_live, "store", self.store):
            load_plugins(
                PluginContext(
                    self.app, self.scheduler, self.auth, Mock(spec=NotificationService), self.auth.configuration
                ),
                (PluginDefinition("bilibili_live", "fxbot.plugins.bilibili_live"),),
            )
            await self.app.start()
        self.addAsyncCleanup(self.app.stop)
        self.bot = OneBot11Client("100", AsyncMock(return_value={"message_id": 1}))
        self.app.register(self.bot, close=AsyncMock())

    async def dispatch(self, text, role="admin", group="300"):
        incoming = payload()
        incoming["message"] = [{"type": "text", "data": {"text": text}}]
        incoming["sender"]["role"] = role
        incoming["group_id"] = int(group)
        return await self.app.router.dispatch(self.bot, parse_event(incoming, bot_id="100"))

    async def test_admin_subscribes_lists_and_unsubscribes_only_current_session(self) -> None:
        with patch("fxbot.plugins.bilibili_live.commands.fetch_room", AsyncMock(return_value=ROOM)):
            result = await self.dispatch("#B站直播订阅 123")
            self.assertEqual(result.failed, ())
            self.assertIsNotNone(self.store.subscription(FIRST))
            await self.dispatch("#B站直播列表", role="member")
            self.assertIn("主播 - 123", self.bot._sender.await_args.args[1]["message"][0]["data"]["text"])
            await self.dispatch("#B站直播取消 123", group="301")
            self.assertIsNotNone(self.store.subscription(FIRST))
            await self.dispatch("#B站直播取消 123")
            self.assertIsNone(self.store.subscription(FIRST))

    async def test_member_cannot_subscribe_or_call_external_query_for_subscription(self) -> None:
        with patch("fxbot.plugins.bilibili_live.commands.fetch_room", AsyncMock()) as fetch:
            result = await self.dispatch("#B站直播订阅 123", role="member")
        self.assertTrue(result.denied)
        fetch.assert_not_awaited()
        self.assertIsNone(self.store.subscription(FIRST))

    async def test_missing_argument_is_reported_without_external_request(self) -> None:
        with patch("fxbot.plugins.bilibili_live.commands.fetch_room", AsyncMock()) as fetch:
            result = await self.dispatch("#B站直播订阅")
        self.assertEqual(result.failed, ())
        fetch.assert_not_awaited()
        text = self.bot._sender.await_args.args[1]["message"][0]["data"]["text"]
        self.assertIn("填写直播间号", text)

    async def test_uid_and_query_commands_use_core_messages(self) -> None:
        with patch("fxbot.plugins.bilibili_live.commands.fetch_room_by_uid", AsyncMock(return_value=ROOM)) as fetch:
            await self.dispatch("/B站直播订阅UID:456")
        fetch.assert_awaited_once_with(456)
        with patch(
            "fxbot.plugins.bilibili_live.commands.fetch_room",
            AsyncMock(return_value=replace(ROOM, cover="https://example.test/cover.png")),
        ):
            result = await self.dispatch("B站直播查询 123", role="member")
        self.assertEqual(result.failed, ())
        self.assertEqual(
            [segment["type"] for segment in self.bot._sender.await_args.args[1]["message"]], ["text", "image"]
        )

    async def test_permission_defaults_are_applied_only_to_new_commands(self) -> None:
        entry = self.permissions["sub_plugins"]["bilibili_live"]["commands"]["bilibili_live_subscribe"]
        self.assertEqual(entry["level"], "admin")
        entry["level"] = "superuser"
        upsert_command_defaults("bilibili_live", "bilibili_live_subscribe", level="admin", storage=self.storage)
        self.assertEqual(entry["level"], "superuser")

    async def test_removed_permission_node_does_not_make_command_public(self) -> None:
        self.permissions["sub_plugins"]["bilibili_live"]["commands"].clear()
        with patch("fxbot.plugins.bilibili_live.commands.fetch_room", AsyncMock()) as fetch:
            result = await self.dispatch("#B站直播订阅 123", role="member")
        self.assertTrue(result.denied)
        fetch.assert_not_awaited()

    async def test_stop_removes_job_and_cancels_startup_calibration(self) -> None:
        self.assertIn("bilibili_live:poll", [job.name for job in self.scheduler.jobs])
        self.assertFalse(any(task.get_name() == "bilibili_live:calibration" for task in self.app._tasks))
        await self.app.stop()
        self.assertFalse(self.app._tasks)
        self.assertNotIn("bilibili_live:poll", [job.name for job in self.scheduler.jobs])


class StandaloneLiveTests(unittest.TestCase):
    """应用入口、协议帧、订阅落盘和开播推送共用真实原生链路。"""

    def test_subscribe_then_push_through_real_websocket_protocol(self) -> None:
        settings = deepcopy(SYSTEM_DEFAULTS)
        settings["chat"]["enabled"] = False
        with tempfile.TemporaryDirectory() as directory:
            store = LiveStore(Path(directory) / "state.json")

            with (
                patch.object(bilibili_live.commands, "store", store),
                patch.object(bilibili_live.commands.live, "store", store),
                patch.object(bilibili_live, "store", store),
                patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(settings)),
                patch.object(bilibili_live.poller, "fetcher", AsyncMock(return_value=ROOM)) as fetch,
                patch.object(bilibili_live.poller, "store", store),
                patch("fxbot.plugins.bilibili_live.commands.fetch_room", AsyncMock(return_value=ROOM)),
            ):
                app = create_app(Settings("test-only", {"onebot11": frozenset({"100"})}))
                with TestClient(app) as client:
                    with client.websocket_connect("/onebot/v11/ws", headers=dict(HEADERS)) as websocket:
                        incoming = payload()
                        incoming["message"] = [{"type": "text", "data": {"text": "#B站直播订阅 123"}}]
                        incoming["sender"]["role"] = "admin"
                        websocket.send_json(incoming)
                        response = websocket.receive_json()
                        self.assertEqual(response["action"], "send_group_msg")
                        self.assertIn("已为本群订阅", response["params"]["message"][0]["data"]["text"])
                        websocket.send_json(
                            {"echo": response["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 901}}
                        )
                        from fxbot.membership.guard import membership_guard

                        notifications = NotificationService(
                            app.state.runtime, get_manager(), app.state.services.authorizer, membership_guard
                        )
                        poller = bilibili_live.poller
                        client.portal.call(poller.calibrate)
                        fetch.return_value = replace(ROOM, live_status=1)
                        delivery = client.portal.start_task_soon(poller.check, notifications)
                        response = websocket.receive_json()
                        self.assertEqual(response["params"]["group_id"], 300)
                        self.assertIn("主播 开播了", response["params"]["message"][0]["data"]["text"])
                        websocket.send_json(
                            {"echo": response["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 902}}
                        )
                        self.assertEqual(delivery.result(timeout=5), 1)
                        self.assertEqual(client.portal.call(poller.check, notifications), 0)
                    self.assertIn(FIRST.key(), store.room_state(123).delivered)
                self.assertFalse(app.state.runtime._tasks)
                self.assertNotIn("bilibili_live:poll", [job.name for job in app.state.services.scheduler.jobs])

    def test_bad_state_rejects_startup_without_overwriting_user_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            original = '{"subscriptions": [], "room_states": {}}'
            path.write_text(original, encoding="utf-8")
            with patch.object(bilibili_live, "store", LiveStore(path)):
                from types import SimpleNamespace

                with self.assertRaisesRegex(ValueError, "版本不受支持"):
                    awaitable = bilibili_live.check_live(SimpleNamespace(notifications=None, first_run=True))
                    asyncio.run(awaitable)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
