"""Provider 元数据和 SDK 工厂的边界测试。"""

from __future__ import annotations

import sys
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from fxbot.config import get_manager
from fxbot.chat.providers.catalog import BUILTIN_PROVIDERS
from fxbot.chat.providers.manager import ProviderManager
from fxbot.chat.providers.register import list_providers, get_provider_class


class ProviderCatalogTests(unittest.TestCase):
    """声明目录不创建 SDK 实例，也不探测无关实现。"""

    def test_declared_capabilities_are_available_without_sdk_imports(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = """
import sys
import importlib.abc
sys.path.insert(0, sys.argv[1])
class RejectSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'openai', 'anthropic', 'google', 'nonebot'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectSDK())
from fxbot.chat.providers.register import list_providers
from fxbot.chat.providers.manager import ProviderManager
from fxbot.config import get_manager
from fxbot.console.routes.ai_providers import _type_options
assert len(list_providers()) == 9
assert len(_type_options()) == 9
assert ProviderManager(get_manager()) is not ProviderManager(get_manager())
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

    def test_selected_factory_does_not_swallow_import_failure(self) -> None:
        error = ModuleNotFoundError("selected-sdk")
        with patch("fxbot.chat.providers.catalog.import_module", side_effect=error) as load:
            with self.assertRaises(ModuleNotFoundError) as caught:
                get_provider_class("anthropic_chat")
        self.assertIs(caught.exception, error)
        load.assert_called_once_with("fxbot.chat.providers.sources.anthropic")

    def test_metadata_snapshot_cannot_modify_registered_defaults(self) -> None:
        snapshot = list_providers()
        snapshot["openai_chat"].default_config["base_url"] = "changed"
        self.assertNotEqual(BUILTIN_PROVIDERS["openai_chat"].default_config["base_url"], "changed")

    def test_managers_do_not_share_mutable_instances(self) -> None:
        first, second = ProviderManager(get_manager()), ProviderManager(get_manager())
        self.assertIsNot(first, second)
        self.assertIsNot(first._providers, second._providers)
