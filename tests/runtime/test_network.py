"""通过真实本机监听验证独立服务的协议收发和关闭。"""

from __future__ import annotations

import json
import time
import socket
import unittest
import threading

import httpx
import uvicorn
from websockets.exceptions import InvalidStatus, ConnectionClosed
from websockets.sync.client import connect

from fxbot.runtime.server import create_app
from fxbot.core.application import ApplicationState
from fxbot.runtime.settings import Settings
from tests.runtime.test_server import HEADERS
from tests.migration.test_onebot11 import payload


class NetworkTests(unittest.TestCase):
    """服务只绑定回环地址和系统分配端口。"""

    def setUp(self) -> None:
        self.app = create_app(Settings("test-only", {"onebot11": frozenset({"100"})}))
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(16)
        port = self.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/onebot/v11/ws"
        self.health = f"http://127.0.0.1:{port}/health"
        config = uvicorn.Config(
            self.app,
            log_level="error",
            log_config=None,
            ws="websockets-sansio",
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started, "本机测试服务器未启动")

    def stop_server(self) -> None:
        """停止本机服务并确认监听线程退出。"""
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.socket.close()
        self.assertFalse(self.thread.is_alive(), "本机测试服务器未关闭")

    def test_real_websocket_round_trip_and_authentication(self) -> None:
        with httpx.Client(trust_env=False) as http:
            self.assertEqual(http.get(self.health).json(), {"ok": True})
        with self.assertRaises(InvalidStatus):
            with connect(self.url, open_timeout=2, close_timeout=1, proxy=None):
                self.fail("未鉴权连接被接受")
        with connect(self.url, additional_headers=HEADERS, open_timeout=2, close_timeout=1, proxy=None) as websocket:
            for _ in range(2):
                websocket.send(json.dumps(payload()))
                action = json.loads(websocket.recv(timeout=2))
                self.assertEqual(action["action"], "send_group_msg")
                self.assertEqual(action["params"]["message"], [{"type": "text", "data": {"text": "pong"}}])
                websocket.send(json.dumps({"echo": action["echo"], "status": "ok", "retcode": 0, "data": {}}))

    def test_server_shutdown_closes_open_websocket_and_lifespan(self) -> None:
        with connect(self.url, additional_headers=HEADERS, open_timeout=2, close_timeout=1, proxy=None) as websocket:
            self.server.should_exit = True
            with self.assertRaises(ConnectionClosed):
                websocket.recv(timeout=3)
        self.thread.join(timeout=5)
        self.assertEqual(self.app.state.runtime.state, ApplicationState.STOPPED)
        self.assertEqual(self.app.state.runtime.bots, ())
