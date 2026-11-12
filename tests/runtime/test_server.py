"""独立网络入口的鉴权、收发和断连测试。"""

from __future__ import annotations

import unittest

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from fxbot.runtime.server import create_app
from fxbot.runtime.settings import Settings
from tests.migration.test_onebot11 import payload

HEADERS = {"authorization": "Bearer test-only", "x-self-id": "100", "x-client-role": "Universal"}


class ServerTests(unittest.TestCase):
    """只使用进程内模拟连接，不访问真实平台。"""

    def test_authenticated_message_round_trip_and_reconnect(self) -> None:
        app = create_app(Settings("test-only", frozenset({"100"})))
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").json(), {"ok": True})
            for _ in range(2):
                with client.websocket_connect("/onebot/v11/ws", headers=HEADERS) as websocket:
                    websocket.send_json(payload())
                    action = websocket.receive_json()
                    self.assertEqual(action["action"], "send_group_msg")
                    self.assertEqual(action["params"]["message"], [{"type": "text", "data": {"text": "pong"}}])
                    websocket.send_json({"status": "ok", "retcode": 0, "data": {}, "echo": action["echo"]})
        self.assertEqual(app.state.runtime.state, "stopped")
        self.assertEqual(app.state.runtime.bots, ())

    def test_missing_wrong_token_identity_and_role_are_rejected(self) -> None:
        app = create_app(Settings("test-only", frozenset({"100"})))
        with TestClient(app) as client:
            for headers in (
                {},
                {**HEADERS, "authorization": "Bearer wrong"},
                {**HEADERS, "x-self-id": "999"},
                {**HEADERS, "x-client-role": "Event"},
            ):
                with self.subTest(headers=headers), self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/onebot/v11/ws", headers=headers):
                        self.fail("无效连接被接受")

    def test_duplicate_connection_does_not_replace_online_bot(self) -> None:
        app = create_app(Settings("test-only", frozenset({"100"})))
        with TestClient(app) as client:
            with client.websocket_connect("/onebot/v11/ws", headers=HEADERS):
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/onebot/v11/ws", headers=HEADERS):
                        self.fail("重复连接被接受")
                self.assertEqual(len(app.state.runtime.bots), 1)

    def test_malformed_protocol_closes_without_error_details(self) -> None:
        app = create_app(Settings("test-only", frozenset({"100"})))
        with TestClient(app) as client:
            with client.websocket_connect("/onebot/v11/ws", headers=HEADERS) as websocket:
                websocket.send_json([])
                with self.assertRaises(WebSocketDisconnect) as caught:
                    websocket.receive_json()
                self.assertEqual(caught.exception.code, 1008)
                self.assertEqual(caught.exception.reason, "")
