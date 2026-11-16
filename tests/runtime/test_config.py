"""项目运行配置的严格读取与命令行边界测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fxbot.runtime.cli import main
from fxbot.runtime.settings import Settings


class RuntimeConfigTests(unittest.TestCase):
    """只读取指定项目配置，不补写身份或读取环境变量。"""

    def test_default_listen_port_is_8683(self) -> None:
        self.assertEqual(Settings("test-only").port, 8683)

    def test_missing_or_empty_token_disables_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for raw in ({}, {"token": ""}):
                path.write_text(json.dumps(raw), encoding="utf-8")
                self.assertEqual(Settings.load(path).token, "")
            self.assertFalse(hasattr(Settings(), "bots"))

    def test_valid_configuration_uses_existing_settings_contract(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"token": "secret", "port": 9000}), encoding="utf-8")
            self.assertEqual(Settings.load(path), Settings("secret", port=9000))

    def test_missing_invalid_and_unknown_fields_are_rejected(self) -> None:
        for raw in (
            [],
            {"token": None},
            {"token": True},
            {"token": " "},
            {"token": " secret"},
            {"bots": {"onebot11": ["100"]}},
            {"token": "x", "bots": "123"},
            {"token": "x", "bots": {"onebot11": [True]}},
            {"token": "x", "onebot11_implementations": {"100": "napcat"}},
            {"token": "x", "typo": 1},
        ):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaises(ValueError):
                    Settings.load(path)

    def test_missing_file_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            with self.assertRaises(FileNotFoundError):
                Settings.load(path)
            self.assertFalse(path.exists())

    def test_cli_applies_only_explicit_listen_overrides(self) -> None:
        settings = Settings("secret")
        with (
            patch("sys.argv", ["fxbot", "--port", "9000"]),
            patch.object(Settings, "load", return_value=settings),
            patch("uvicorn.run") as run,
        ):
            main()
        self.assertEqual(run.call_args.kwargs["port"], 9000)

    def test_invalid_override_is_not_silently_replaced_by_default(self) -> None:
        settings = Settings("secret")
        with (
            patch("sys.argv", ["fxbot", "--port", "0"]),
            patch.object(Settings, "load", return_value=settings),
            patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            main()
