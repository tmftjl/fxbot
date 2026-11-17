"""标准 src 包布局与构建配置回归。"""

from __future__ import annotations

import ast
import fnmatch
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement

PLUGIN_DISTRIBUTIONS = {
    "arclet": "arclet-alconna",
    "matplotlib": "matplotlib",
    "dateutil": "python-dateutil",
    "yaml": "pyyaml",
    "rapidfuzz": "rapidfuzz",
    "bilibili_api": "bilibili-api-python",
    "emoji": "emoji",
}


def _pyproject(root: Path) -> dict:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


class PackageLayoutTests(unittest.TestCase):
    """所有应用源码只以 fxbot 包命名空间交付。"""

    def test_package_discovery_uses_src(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = root / "src"
        packages = {
            path.parent.relative_to(source).as_posix().replace("/", ".") for path in source.rglob("__init__.py")
        }
        self.assertIn("fxbot", packages)
        self.assertIn("fxbot.platforms", packages)
        self.assertIn("fxbot.runtime", packages)
        self.assertIn("fxbot.chat.amemorix.core", packages)
        self.assertTrue(all(name == "fxbot" or name.startswith("fxbot.") for name in packages))
        self.assertFalse(any(name.startswith(("fxbot.tests", "fxbot.data", "fxbot.temp")) for name in packages))
        self.assertFalse((root / "__init__.py").exists())
        self.assertFalse((root / "setup.py").exists())

    def test_build_uses_pdm_backend_and_keeps_package_assets(self) -> None:
        root = Path(__file__).resolve().parents[1]
        configuration = _pyproject(root)
        self.assertEqual(configuration["build-system"]["build-backend"], "pdm.backend")
        self.assertNotIn("setuptools", configuration.get("tool", {}))
        excludes = configuration.get("tool", {}).get("pdm", {}).get("build", {}).get("excludes", [])
        self.assertTrue(any("node_modules" in pattern for pattern in excludes))

        package = root / "src" / "fxbot"
        expected: set[Path] = set()
        for directory in ("resources", "resource", "prompts", "web/dist"):
            folder = package / directory
            if folder.is_dir():
                expected.update(path for path in folder.rglob("*") if path.is_file())
        for pattern in ("*.json", "*.cjs", "*.md", "LICENSE", "requirements.txt"):
            expected.update(
                path for path in package.rglob(pattern) if path.is_file() and "node_modules" not in path.parts
            )
        self.assertTrue(expected)

        for path in sorted(expected):
            relative = path.relative_to(root).as_posix()
            with self.subTest(path=relative):
                self.assertFalse(
                    any(
                        relative == pattern
                        or relative.startswith(pattern.rstrip("/") + "/")
                        or fnmatch.fnmatch(relative, pattern)
                        for pattern in excludes
                    ),
                    relative,
                )

    def test_plugin_dependencies_are_declared_by_their_owners(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = _pyproject(root)
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
