"""插件依赖检查、批量安装和冲突报告边界。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fxbot.runtime import plugin_dependencies


class PluginDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "requirements.txt").write_text("missing-pkg>=1\n", encoding="utf-8")
        (self.root / "beta").mkdir()
        (self.root / "beta" / "requirements.txt").write_text(
            "loguru>=0.7.3\ninvalid requirement here\n# comment\n", encoding="utf-8"
        )
        self.installed = {"loguru": "0.7.3"}

    def test_collects_missing_and_reports_owners(self) -> None:
        with patch.object(plugin_dependencies, "installed_versions", return_value=self.installed):
            missing, owners = plugin_dependencies.collect_missing(("alpha", "beta"), self.root)
        self.assertEqual(missing, ["missing-pkg>=1"])
        self.assertEqual(owners["missing-pkg>=1"], ("alpha",))

    def test_satisfied_dependency_is_not_installed(self) -> None:
        with (
            patch.object(plugin_dependencies, "installed_versions", return_value=self.installed),
            patch.object(plugin_dependencies.subprocess, "Popen") as run,
        ):
            plugin_dependencies.ensure_plugin_dependencies(("beta",), self.root)
        run.assert_not_called()

    def test_missing_dependency_uses_uv_pip_with_current_python(self) -> None:
        with (
            patch.object(plugin_dependencies, "installed_versions", return_value=self.installed),
            patch.object(plugin_dependencies.shutil, "which", return_value="/usr/bin/uv"),
            patch.object(plugin_dependencies.subprocess, "Popen") as run,
        ):
            run.return_value.returncode = 0
            run.return_value.communicate.return_value = ("ok", "")
            plugin_dependencies.ensure_plugin_dependencies(("alpha",), self.root)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["/usr/bin/uv", "pip", "install", "--python"])
        self.assertEqual(command[4], sys.executable)
        self.assertEqual(command[-1], "missing-pkg>=1")

    def test_without_uv_falls_back_to_current_interpreter_pip(self) -> None:
        with (
            patch.object(plugin_dependencies, "installed_versions", return_value=self.installed),
            patch.object(plugin_dependencies.shutil, "which", return_value=None),
            patch.object(plugin_dependencies.subprocess, "Popen") as run,
        ):
            run.return_value.returncode = 0
            run.return_value.communicate.return_value = ("", "")
            plugin_dependencies.ensure_plugin_dependencies(("alpha",), self.root)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(command[-1], "missing-pkg>=1")

    def test_conflict_output_is_reported_without_raising(self) -> None:
        with (
            patch.object(plugin_dependencies, "installed_versions", return_value=self.installed),
            patch.object(plugin_dependencies.shutil, "which", return_value="/usr/bin/uv"),
            patch.object(plugin_dependencies.subprocess, "Popen") as run,
            patch.object(plugin_dependencies.logger, "error") as error,
        ):
            run.return_value.returncode = 1
            run.return_value.communicate.return_value = (
                "",
                "Cannot install packages because these package versions have conflicting dependencies.",
            )
            self.assertFalse(plugin_dependencies.install_packages(["missing-pkg>=1"]))
        self.assertIn("依赖冲突", error.call_args.args[0])

    def test_install_timeout_terminates_and_reaps_without_retry(self):
        with (
            patch.object(plugin_dependencies.subprocess, "Popen") as launch,
            patch.object(plugin_dependencies, "terminate_process") as terminate,
            patch.object(plugin_dependencies.importlib, "invalidate_caches") as invalidate,
        ):
            process = launch.return_value
            process.returncode = None
            process.communicate.side_effect = [
                plugin_dependencies.subprocess.TimeoutExpired("installer", 180),
                ("", ""),
            ]

            def kill(child):
                child.returncode = -1

            terminate.side_effect = kill
            self.assertFalse(plugin_dependencies.install_packages(["missing-pkg"]))
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(launch.call_args.kwargs["stdin"], plugin_dependencies.subprocess.DEVNULL)
            self.assertEqual(
                process.communicate.call_args_list[0].kwargs["timeout"], plugin_dependencies._INSTALL_TIMEOUT_SECONDS
            )
            self.assertEqual(
                process.communicate.call_args_list[1].kwargs["timeout"], plugin_dependencies._CLOSE_TIMEOUT_SECONDS
            )
            terminate.assert_called_once_with(process)
            process.stdout.close.assert_called_once()
            process.stderr.close.assert_called_once()
            invalidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
