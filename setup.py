"""从仓库内发现应用包和独立核心包。"""

from __future__ import annotations

from pathlib import Path

from setuptools import setup, find_packages

SOURCE_ROOT = str(Path(__file__).resolve().parent)

APPLICATION_PACKAGES = (
    "adapter",
    "chat",
    "config",
    "console",
    "db",
    "membership",
    "permission",
    "plugin",
    "plugins",
    "system_control",
    "utils",
)

setup(
    packages=["fxbot"]
    + [f"fxbot.{name}" for name in find_packages(SOURCE_ROOT) if name.split(".", 1)[0] in APPLICATION_PACKAGES]
    + find_packages(SOURCE_ROOT, include=["core", "core.*"]),
)
