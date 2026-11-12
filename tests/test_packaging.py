"""标准 src 包布局与构建配置回归。"""

from __future__ import annotations

import unittest
from pathlib import Path

from setuptools import find_packages


class PackageLayoutTests(unittest.TestCase):
    """所有应用源码只以 fxbot 包命名空间交付。"""

    def test_package_discovery_uses_src(self) -> None:
        root = Path(__file__).resolve().parents[1]
        packages = find_packages(str(root / "src"))
        self.assertIn("fxbot", packages)
        self.assertIn("fxbot.core.platforms", packages)
        self.assertIn("fxbot.runtime", packages)
        self.assertIn("fxbot.chat.amemorix.core", packages)
        self.assertTrue(all(name == "fxbot" or name.startswith("fxbot.") for name in packages))
        self.assertFalse(any(name.startswith(("fxbot.tests", "fxbot.data", "fxbot.temp")) for name in packages))
        self.assertFalse((root / "__init__.py").exists())
        self.assertFalse((root / "setup.py").exists())
