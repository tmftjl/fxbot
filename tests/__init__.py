"""FxBot 标准库测试包。"""

from __future__ import annotations

import sys
import atexit
import shutil
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

# 源码测试显式使用 src 布局，不依赖仓库父目录的包名。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 所有测试共享隔离的数据根，任何入口均不能写入项目已有数据。
_test_project = Path(tempfile.mkdtemp(prefix="fxbot-tests-"))
_project_patch = patch("fxbot.utils.paths.project_root", return_value=_test_project)
# 使用上下文模式，避免旧测试中的 patch.stopall() 解除全局数据隔离。
_project_patch.__enter__()


def _cleanup_test_project() -> None:
    """关闭测试数据库连接后再清理隔离目录。"""
    log_store = sys.modules.get("fxbot.console.log_store")
    if log_store is not None:
        log_store.shutdown()
    database = sys.modules.get("fxbot.db.base_models")
    if database is not None and database.engine is not None:
        asyncio.run(database.engine.dispose())
    _project_patch.__exit__(None, None, None)
    try:
        shutil.rmtree(_test_project)
    except PermissionError:
        # 旧业务仍有未托管连接；保留临时目录供排查，不强删打开的数据库。
        print(f"测试临时数据仍被占用，保留目录: {_test_project}", file=sys.stderr)


atexit.register(_cleanup_test_project)
