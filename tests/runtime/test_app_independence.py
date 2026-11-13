"""完整独立入口不得因控制台访问而加载旧框架。"""

from __future__ import annotations

import sys
import unittest
import subprocess
from pathlib import Path


class ApplicationIndependenceTests(unittest.TestCase):
    """在新解释器中拒绝宿主和旧适配层导入。"""

    def test_application_console_and_shutdown_do_not_import_nonebot(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = """
import sys
import tempfile
import importlib.abc
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
class RejectLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        legacy = fullname.startswith(('nonebot', 'fxbot.adapter', 'fxbot.plugin.'))
        sdk = fullname.split('.')[0] in {'openai', 'anthropic', 'google'}
        if legacy or sdk:
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectLegacy())
from fastapi.testclient import TestClient
with tempfile.TemporaryDirectory() as directory:
    with patch('fxbot.utils.paths.project_root', return_value=Path(directory)):
        from fxbot.runtime.settings import Settings
        from fxbot.runtime.server import create_app
        from fxbot.console import auth, log_store, log_stream
        from fxbot.db import base_models
        app = create_app(Settings('test-token', {'onebot11': frozenset({'100'})}))
        with patch.object(auth, 'get_console_token', return_value='console-token'), TestClient(app) as client:
            for path in ('/health', '/fxbot/health', '/fxbot'):
                assert client.get(path).status_code == 200, path
            headers = {'authorization': 'Bearer console-token'}
            for path in ('/fxbot/bots', '/fxbot/config/tabs', '/fxbot/membership/groups',
                         '/fxbot/chat/health', '/fxbot/chat/tenants', '/fxbot/local-chat/options',
                         '/fxbot/amemorix/status?tenant=onebot11:100:group:300'):
                response = client.get(path, headers=headers)
                assert response.status_code == 200, (path, response.text)
        assert log_store._writer is None
        assert log_stream._sink_id is None
        assert base_models.engine is None
assert not any(name.startswith('nonebot') for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c", script, str(root / "src")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
