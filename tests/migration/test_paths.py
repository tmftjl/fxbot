"""自带资源和项目运行数据的路径边界测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from fxbot.utils import paths


class PathTests(unittest.TestCase):
    """下载内容只进入项目数据目录，不依赖宿主框架。"""

    def test_legacy_patch_cleanup_cannot_disable_data_isolation(self) -> None:
        isolated_root = paths.project_root()
        patch.stopall()
        self.assertEqual(paths.project_root(), isolated_root)

    def test_downloads_config_and_cache_use_project_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(paths, "project_root", return_value=root):
                self.assertEqual(paths.data_dir("rocom") / "resources", root / "data/rocom/resources")
                self.assertEqual(paths.config_dir(), root / "data/config")
                self.assertEqual(paths.cache_dir(), root / "data/cache")
                self.assertEqual(paths.database_path(), root / "data/db/fxbot.db")
                with self.assertRaises(ValueError):
                    paths.data_dir("../outside")

    def test_source_project_is_independent_of_working_directory(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = (
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "from fxbot.utils.paths import project_root; "
            "assert project_root() == Path(sys.argv[2]); "
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script, str(root / "src"), str(root)],
                cwd=directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
