"""控制台适配器配置的读取、校验、并发和热加载测试。"""

from __future__ import annotations

import json
import unittest
from hashlib import sha1
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from fxbot.console import auth
from fxbot.config.storage import ConfigStorage
from fxbot.runtime.server import create_app
from fxbot.runtime.settings import Settings
from tests.runtime.test_server import HEADERS

APP_ID = "123456789"


class AdapterConfigTests(unittest.TestCase):
    """配置页只通过认证路由读写运行时配置，失败时不落盘。"""

    def setUp(self) -> None:
        directory = TemporaryDirectory(prefix="fxbot-adapters-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.json"
        self.storage = ConfigStorage("runtime", path=self.path)
        self.path.write_text(
            json.dumps({"token": "runtime-token", "host": "127.0.0.1", "port": 8683}),
            encoding="utf-8",
        )
        self.app = create_app(Settings("test-only"))
        self.client = self.enterContext(TestClient(self.app, raise_server_exceptions=False))
        self.addCleanup(patch.stopall)
        patch.object(auth, "get_console_token", return_value="console-only").start()
        patch("fxbot.console.routes.adapters._storage", return_value=self.storage).start()
        self.headers = {"authorization": "Bearer console-only"}

    def _get(self) -> dict:
        response = self.client.get("/fxbot/adapters", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _put(self, config: dict, revision: str | None = None) -> object:
        return self.client.put(
            "/fxbot/adapters",
            headers=self.headers,
            json={"config": config, "base_revision": revision if revision is not None else self._get()["revision"]},
        )

    def test_requires_console_authentication(self) -> None:
        self.assertEqual(self.client.get("/fxbot/adapters").status_code, 401)
        self.assertEqual(self.client.get("/fxbot/adapters", headers={"authorization": "Bearer wrong"}).status_code, 403)

    def test_reads_file_config_and_content_revision(self) -> None:
        data = self._get()
        self.assertEqual(data["config"]["token"], "runtime-token")
        self.assertEqual(data["config"]["port"], 8683)
        self.assertEqual(data["config"]["qq_bots"], {})
        self.assertIsNone(data["warning"])
        self.assertEqual(data["revision"], sha1(self.path.read_bytes()).hexdigest())

    def test_missing_file_falls_back_to_running_settings(self) -> None:
        self.path.unlink()
        data = self._get()
        self.assertEqual(data["config"]["token"], "test-only")
        self.assertEqual(data["revision"], "")
        self.assertIn("不存在", data["warning"])

    def test_invalid_payload_is_rejected_without_writing(self) -> None:
        original = self.path.read_bytes()
        config = self._get()["config"]
        cases = (
            {**config, "unknown": True},
            {**config, "port": 70000},
            {**config, "qq_bots": {APP_ID: {"secret": 1}}},
            {**config, "qq_bots": {APP_ID: {"secret": ""}}},
            {**config, "qq_bots": {APP_ID: {"secret": "value", "mode": "invalid"}}},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                response = self._put(payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.path.read_bytes(), original)

    def test_stale_revision_is_rejected(self) -> None:
        config = self._get()["config"]
        response = self._put(config, revision="stale")
        self.assertEqual(response.status_code, 409)

    def test_save_persists_config_and_rotates_onebot_token(self) -> None:
        config = self._get()["config"]
        config["token"] = "updated-token"
        with self.client.websocket_connect("/onebot/v11/ws", headers=dict(HEADERS)) as old:
            response = self._put(config)
            self.assertEqual(response.status_code, 200, response.text)
            # 令牌轮换会主动关闭旧连接，客户端必须用新令牌重新握手。
            with self.assertRaises(WebSocketDisconnect):
                old.receive_json()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["token"], "updated-token")
        self.assertEqual(self.app.state.settings.current.token, "updated-token")

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/onebot/v11/ws", headers=dict(HEADERS)):
                self.fail("旧令牌仍然被接受")
        with self.client.websocket_connect(
            "/onebot/v11/ws", headers={**HEADERS, "authorization": "Bearer updated-token"}
        ) as websocket:
            self.assertIsNotNone(websocket)

    def test_qq_bot_changes_rebuild_connections(self) -> None:
        config = self._get()["config"]
        config["qq_bots"] = {APP_ID: {"secret": "app-secret", "mode": "webhook"}}
        response = self._put(config)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(APP_ID, self.app.state.services.qq.connections)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["qq_bots"][APP_ID]["secret"], "app-secret")

        config = response.json()["config"]
        config["qq_bots"] = {}
        response = self._put(config)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(APP_ID, self.app.state.services.qq.connections)


if __name__ == "__main__":
    unittest.main()
