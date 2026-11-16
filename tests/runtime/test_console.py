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
        for path in ("bots", "membership/groups", "config", "permissions", "cache"):
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

    def test_plugin_metadata_and_command_names_are_chinese(self) -> None:
        plugins = self.client.get("/fxbot/plugins", headers=self.headers)
        self.assertEqual(plugins.status_code, 200)
        self.assertEqual(
            plugins.json(),
            {
                "bilibili_live": "B站直播推送",
                "group_analysis": "群聊日报",
                "group_admin": "群管理",
                "entertain": "娱乐",
                "video_parser": "视频解析",
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
