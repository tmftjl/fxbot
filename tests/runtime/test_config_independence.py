"""配置基础设施不得隐式加载宿主框架和业务模块。"""

from __future__ import annotations

import sys
import unittest
import subprocess
from pathlib import Path


class ConfigIndependenceTests(unittest.TestCase):
    """在全新解释器中拒绝框架导入并验证完整保存流程。"""

    def test_bootstrap_and_save_do_not_load_business_or_framework(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = """
import sys
import tempfile
import importlib.abc
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
class RejectFramework(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        framework = fullname.split('.')[0] in {'nonebot', 'nonebot_plugin_apscheduler'}
        business = fullname.startswith(('fxbot.chat', 'fxbot.membership'))
        if framework or business:
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectFramework())
from fxbot.config.manager import ConfigManager
with tempfile.TemporaryDirectory() as directory, patch('fxbot.utils.paths.project_root', return_value=Path(directory)):
    manager = ConfigManager()
    manager.bootstrap()
    calls = []
    manager.subscribe(lambda: calls.append('saved'))
    config = manager.get_system()
    config['chat']['max_context_size'] = 31
    manager.save_console_configs({'system': config})
    assert manager.get_system()['chat']['max_context_size'] == 31
    assert calls == ['saved']
assert not any(name == 'nonebot' or name.startswith('nonebot.') for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c", script, str(root / "src")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
