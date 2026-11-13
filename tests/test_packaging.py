"""标准 src 包布局与构建配置回归。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from setuptools import find_packages
from packaging.requirements import Requirement
from setuptools.config.pyprojecttoml import read_configuration

PLUGIN_DISTRIBUTIONS = {
    "arclet": "arclet-alconna",
    "matplotlib": "matplotlib",
    "dateutil": "python-dateutil",
    "yaml": "pyyaml",
    "rapidfuzz": "rapidfuzz",
    "bilibili_api": "bilibili-api-python",
    "emoji": "emoji",
}


class PackageLayoutTests(unittest.TestCase):
    """所有应用源码只以 fxbot 包命名空间交付。"""

    def test_package_discovery_uses_src(self) -> None:
        root = Path(__file__).resolve().parents[1]
        packages = find_packages(str(root / "src"))
        self.assertIn("fxbot", packages)
        self.assertIn("fxbot.platforms", packages)
        self.assertIn("fxbot.runtime", packages)
        self.assertIn("fxbot.chat.amemorix.core", packages)
        self.assertTrue(all(name == "fxbot" or name.startswith("fxbot.") for name in packages))
        self.assertFalse(any(name.startswith(("fxbot.tests", "fxbot.data", "fxbot.temp")) for name in packages))
        self.assertFalse((root / "__init__.py").exists())
        self.assertFalse((root / "setup.py").exists())

    def test_package_data_uses_one_shared_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        configuration = read_configuration(root / "pyproject.toml")
        data = configuration["tool"]["setuptools"]["package-data"]
        self.assertEqual(set(data), {""})
        patterns = data[""]
        for name in find_packages(str(root / "src")):
            package = root / "src" / name.replace(".", "/")
            selected = {path for pattern in patterns for path in package.glob(pattern) if path.is_file()}
            expected = set()
            for directory in ("resources", "resource", "prompts", "web/dist"):
                folder = package / directory
                if folder.is_dir():
                    expected.update(path for path in folder.rglob("*") if path.is_file())
            for pattern in ("*.json", "*.cjs", "LICENSE", "requirements.txt"):
                expected.update(package.glob(pattern))
            with self.subTest(package=name):
                self.assertFalse(expected - selected, expected - selected)

    def test_plugin_dependencies_are_declared_by_their_owners(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = read_configuration(root / "pyproject.toml")
        core = {Requirement(value).name.lower() for value in config["project"]["dependencies"]}
        self.assertFalse(core & set(PLUGIN_DISTRIBUTIONS.values()))
        for source in (root / "src/fxbot").rglob("*.py"):
            if source.is_relative_to(root / "src/fxbot/plugins"):
                continue
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
                names = (
                    [item.name for item in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if isinstance(node, ast.ImportFrom) and not node.level and node.module else [])
                )
                for name in names:
                    self.assertNotIn(name.split(".")[0], PLUGIN_DISTRIBUTIONS, str(source))
        self.assertEqual((root / "requirements.txt").read_text(encoding="utf-8").strip(), ".")
        for plugin in (root / "src/fxbot/plugins").iterdir():
            if not (plugin / "__init__.py").is_file():
                continue
            imported = set()
            for source in plugin.rglob("*.py"):
                for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
                    if isinstance(node, ast.Import):
                        imported.update(item.name.split(".")[0] for item in node.names)
                    elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                        imported.add(node.module.split(".")[0])
            expected = {PLUGIN_DISTRIBUTIONS[name] for name in imported & PLUGIN_DISTRIBUTIONS.keys()}
            manifest = plugin / "requirements.txt"
            declared = (
                {
                    Requirement(line).name.lower()
                    for line in manifest.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
                if manifest.exists()
                else set()
            )
            with self.subTest(plugin=plugin.name):
                self.assertEqual(expected, declared)
