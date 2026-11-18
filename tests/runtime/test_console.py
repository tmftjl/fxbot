"""独立控制台的鉴权、作用域、关闭和错误边界测试。"""

from __future__ import annotations

import logging
import sqlite3
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from loguru import logger
from fastapi.testclient import TestClient

from fxbot.core import Message, SessionTarget
from fxbot.console import auth, account, log_store, log_stream
from fxbot.core.errors import ActionFailed
from fxbot.runtime.server import create_app
from fxbot.runtime.settings import Settings
from fxbot.membership.service import membership_service
from tests.runtime.test_server import HEADERS


class ConsoleTests(unittest.TestCase):
    """所有请求都落在新 ASGI 应用，不初始化外部框架。"""

    def setUp(self) -> None:
        self.app = create_app(Settings("test-only"))
        self.client = self.enterContext(TestClient(self.app, raise_server_exceptions=False))
        self.enterContext(patch.object(auth, "get_console_token", return_value="console-only"))
        self.headers = {"authorization": "Bearer console-only"}

    def test_protected_routes_share_the_same_bearer_boundary(self) -> None:
        for path in ("bots", "membership/groups", "config", "permissions", "cache", "stats/today"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(f"/fxbot/{path}").status_code, 401)
                self.assertEqual(
                    self.client.get(f"/fxbot/{path}", headers={"authorization": "Bearer wrong"}).status_code, 403
                )
                self.assertEqual(self.client.get(f"/fxbot/{path}", headers=self.headers).status_code, 200)

    def test_plugin_schema_discovery_does_not_start_plugin_entrypoints(self) -> None:
        with patch("importlib.util.spec_from_file_location", side_effect=AssertionError("schema re-import")):
            response = self.client.get("/fxbot/config/tabs", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("video_parser", {item["key"] for item in response.json()})

    def test_system_config_tab_exposes_superusers(self) -> None:
        tabs = {item["key"]: item for item in self.client.get("/fxbot/config/tabs", headers=self.headers).json()}
        cards = {card["key"]: card for card in tabs["system"]["cards"]}
        fields = {field["field"]: field for field in cards["system"]["schemas"]}
        self.assertEqual(fields["superusers"]["component"], "GTags")
        self.assertEqual(fields["superusers"]["default"], [])
        self.assertIn("用户 ID", fields["superusers"]["helpMessage"])

    def test_invalid_permission_update_is_rejected_without_changing_existing_rules(self) -> None:
        from copy import deepcopy

        original = self.client.get("/fxbot/permissions", headers=self.headers).json()
        changed = deepcopy(original)
        changed["top"]["level"] = "superusr"
        response = self.client.put("/fxbot/permissions", headers=self.headers, json=changed)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/fxbot/permissions", headers=self.headers).json(), original)

    def test_memory_route_translates_domain_errors_but_does_not_expose_programming_errors(self) -> None:
        from fxbot.console.services import memory_admin
        from fxbot.console.services.errors import AdminFailure, MemoryAdminError

        with patch.object(
            memory_admin,
            "status",
            AsyncMock(side_effect=MemoryAdminError(kind=AdminFailure.UNAVAILABLE, detail="disabled")),
        ):
            response = self.client.get("/fxbot/amemorix/status?tenant=test", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "disabled")
        with patch.object(memory_admin, "status", AsyncMock(side_effect=RuntimeError("secret-programming-error"))):
            response = self.client.get("/fxbot/amemorix/status?tenant=test", headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret-programming-error", response.text)

    def test_stats_endpoints_return_frontend_shapes(self) -> None:
        today = self.client.get("/fxbot/stats/today", headers=self.headers)
        self.assertEqual(today.status_code, 200)
        self.assertIsInstance(today.json().get("bots"), dict)

        history = self.client.get("/fxbot/stats/history?days=3", headers=self.headers)
        self.assertEqual(history.status_code, 200)
        body = history.json()
        self.assertEqual(body["days"], 3)
        self.assertEqual(len(body["items"]), 3)
        self.assertEqual(set(body["items"][0]), {"date", "sent", "group", "private"})

        usage = self.client.get("/fxbot/stats/model_usage?hours=24", headers=self.headers)
        self.assertEqual(usage.status_code, 200)
        self.assertEqual(usage.json()["hours"], 24)
        self.assertIn("tasks", usage.json())

    def test_persona_crud_and_knowledge_is_explicitly_disabled(self) -> None:
        listed = self.client.get("/fxbot/ai_chat/personas", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertIn("default", listed.json())
        self.assertIsInstance(listed.json()["items"], dict)

        key = "console_test_persona"
        created = self.client.post(
            "/fxbot/ai_chat/persona", headers=self.headers, json={"key": key, "desc": "测试人格"}
        )
        self.assertEqual(created.status_code, 200)
        self.assertIn(key, self.client.get("/fxbot/ai_chat/personas", headers=self.headers).json()["items"])
        updated = self.client.put(f"/fxbot/ai_chat/persona/{key}", headers=self.headers, json={"desc": "更新后"})
        self.assertEqual(updated.status_code, 200)
        deleted = self.client.delete(f"/fxbot/ai_chat/persona/{key}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn(key, self.client.get("/fxbot/ai_chat/personas", headers=self.headers).json()["items"])

        stats = self.client.get(f"/fxbot/ai_chat/knowledge/{key}/stats", headers=self.headers)
        self.assertEqual(stats.status_code, 501)

    def test_plugin_metadata_and_command_names_are_chinese(self) -> None:
        plugins = self.client.get("/fxbot/plugins", headers=self.headers)
        self.assertEqual(plugins.status_code, 200)
        self.assertEqual(
            plugins.json(),
            {
                "bilibili_live": "B站直播推送",
                "cultured": "图库",
                "entertain": "娱乐",
                "group_admin": "群管理",
                "group_analysis": "群聊日报",
                "gscore": "gscore 连接器",
                "help": "帮助",
                "infinite_gen2": "无限二代",
                "memes": "表情包制作",
                "rocom": "洛克王国",
                "server_admin": "服务器运维",
                "useful": "实用工具",
                "video_parser": "视频解析",
                "wuwa_emoji": "鸣潮表情",
            },
        )
        commands = self.client.get("/fxbot/commands", headers=self.headers).json()
        self.assertEqual(commands["entertain"]["like"], "QQ点赞")
        self.assertEqual(commands["bilibili_live"]["bilibili_live_subscribe"], "订阅B站直播")
        details = self.client.get("/fxbot/plugins/details", headers=self.headers).json()
        self.assertIn("usage", details["group_admin"])
        self.assertEqual(self.client.get("/fxbot/plugins").status_code, 401)

    def test_online_bots_come_from_current_application(self) -> None:
        with self.client.websocket_connect("/onebot/v11/ws", headers=dict(HEADERS)):
            data = self.client.get("/fxbot/bots", headers=self.headers).json()
            self.assertEqual(data, {"bots": [{"self_id": "100", "adapter": "onebot11"}]})

    def test_chat_memory_and_web_channel_are_mounted_with_authentication(self) -> None:
        paths = ("chat/health", "chat/tenants", "local-chat/options", "amemorix/status?tenant=onebot11:100:group:300")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(f"/fxbot/{path}").status_code, 401)
                response = self.client.get(f"/fxbot/{path}", headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
        health = self.client.get("/fxbot/chat/health", headers=self.headers).json()
        self.assertTrue(health["storage_ready"])
        self.assertTrue(health["scheduler_ready"])

    def test_frontend_and_public_health_are_served(self) -> None:
        response = self.client.get("/fxbot")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(self.client.get("/fxbot/health").json(), {"ok": True})

    def test_unknown_runtime_failure_returns_generic_error(self) -> None:
        with patch.object(membership_service, "list_groups", side_effect=RuntimeError("secret path and credentials")):
            response = self.client.get("/fxbot/membership/groups", headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "服务暂时不可用"})

    def test_corrupt_account_during_password_change_is_not_validation_error(self) -> None:
        with patch.object(account, "change_password", side_effect=ValueError("broken internal account path")):
            response = self.client.put(
                "/fxbot/auth/password",
                headers=self.headers,
                json={"old_password": "old", "new_password": "new-value"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "服务暂时不可用"})

    def test_log_database_failure_is_not_reported_as_empty_history(self) -> None:
        log_store.flush()
        with patch.object(log_store, "_connect", side_effect=sqlite3.DatabaseError("internal database secret")):
            response = self.client.get("/fxbot/logs/history", headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("internal database", response.text)

    def test_legacy_bare_membership_group_uses_managed_bot(self) -> None:
        send = AsyncMock()
        bot = SimpleNamespace(platform="onebot11", bot_id="100", send=send)
        self.client.portal.call(lambda: self.app.state.runtime.register(bot, close=AsyncMock()))
        row = SimpleNamespace(group_id="757463664", managed_by_bot="100", expires_at=None)
        with patch.object(membership_service, "get_group", return_value=row):
            response = self.client.post(
                "/fxbot/membership/remind",
                headers=self.headers,
                json={"group_id": "757463664"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        target, _ = send.await_args.args
        self.assertEqual(target, SessionTarget("onebot11", "100", "group", "757463664"))

    def test_message_stats_counts_successful_group_sends_and_ignores_channels(self) -> None:
        bot_id = "stats-bot"
        target = SessionTarget("onebot11", bot_id, "group", "stats-group")
        channel = SessionTarget("onebot11", bot_id, "channel", "stats-channel")
        send = AsyncMock()
        bot = SimpleNamespace(platform="onebot11", bot_id=bot_id, send=send)
        self.client.portal.call(lambda: self.app.state.runtime.register(bot, close=AsyncMock()))

        async def deliver() -> None:
            await bot.send(target, Message.text("a"))
            await bot.send(target, Message.text("b"))
            await bot.send(channel, Message.text("channel"))
            send.side_effect = ActionFailed("failed")
            with self.assertRaises(ActionFailed):
                await bot.send(target, Message.text("failed"))

        self.client.portal.call(deliver)
        data = self.client.get("/fxbot/stats/today", headers=self.headers).json()
        self.assertEqual(
            data["bots"][bot_id],
            {"total_sent": 2, "group": {"count": 2, "targets": {"stats-group": 2}}},
        )

    def test_notify_counts_unknown_offline_and_send_failures(self) -> None:
        online = SimpleNamespace(
            platform="onebot11",
            bot_id="100",
            send=AsyncMock(side_effect=ActionFailed("upstream secret")),
        )
        self.client.portal.call(lambda: self.app.state.runtime.register(online, close=AsyncMock()))
        records = {
            "onebot11:100:group:1": SimpleNamespace(group_id="onebot11:100:group:1"),
            "onebot11:200:group:2": SimpleNamespace(group_id="onebot11:200:group:2"),
        }
        with patch.object(membership_service, "get_group", side_effect=lambda key: records.get(key)):
            response = self.client.post(
                "/fxbot/membership/notify",
                headers=self.headers,
                json={"group_ids": [*records, "missing"], "text": "notice"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"sent": 0, "failed": 3})


class ResourceOwnershipTests(unittest.TestCase):
    """日志线程和缓冲都必须由应用显式回收。"""

    def test_second_application_cannot_take_over_existing_log_resources(self) -> None:
        settings = Settings("test-only")
        first = create_app(settings)
        with TestClient(first) as client:
            sink_id = log_stream._sink_id
            second = create_app(settings)
            with self.assertRaisesRegex(RuntimeError, "所有者"):
                with TestClient(second):
                    self.fail("重复的资源所有者被接受")
            self.assertEqual(log_stream._sink_id, sink_id)
            self.assertEqual(client.get("/health").json(), {"ok": True})

    def test_shutdown_closes_log_writer_and_removes_only_owned_sink(self) -> None:
        observed = []
        external_sink = logger.add(observed.append, format="{message}")
        self.addCleanup(logger.remove, external_sink)
        app = create_app(Settings("test-only"))
        with TestClient(app):
            logger.info("test-owned-log")
            logging.getLogger("test.core").warning("test-core-log")
            log_store.flush()
            writer = log_store._writer
            self.assertIsNotNone(writer)
            self.assertTrue(any("test-core-log" in item["text"] for item in log_stream.snapshot()))
        self.assertFalse(writer.is_alive())
        self.assertIsNone(writer._conn)
        self.assertIsNone(log_stream._sink_id)
        logger.info("external-sink-still-active")
        self.assertTrue(any("external-sink-still-active" in item for item in observed))

    def test_invalid_existing_account_is_not_reset(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(account, "_path", return_value=path), self.assertRaises(ValueError):
                account.ensure_account()
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")
