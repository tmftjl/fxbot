"""应用和独立核心的包发现回归。"""

from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from setuptools import find_packages


class PackageLayoutTests(unittest.TestCase):
    """构建只收录应用源码，不依赖仓库的父目录。"""

    def test_package_discovery_uses_repository_root(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with patch("setuptools.setup") as setup:
            runpy.run_path(str(root / "setup.py"), run_name="__main__")
        packages = setup.call_args.kwargs["packages"]
        self.assertIn("fxbot", packages)
        self.assertIn("fxbot.chat.amemorix.core", packages)
        self.assertIn("core.platforms", packages)
        self.assertEqual(len(packages), len(set(packages)))
        self.assertFalse(
            any(name.startswith(("fxbot.core", "fxbot.tests", "fxbot.data", "fxbot.temp")) for name in packages)
        )
        expected = {
            f"fxbot.{name}"
            for name in find_packages(str(root))
            if not name.startswith(("core", "tests", "scripts", "build", "temp"))
        }
        self.assertTrue(expected.issubset(packages), expected.difference(packages))
