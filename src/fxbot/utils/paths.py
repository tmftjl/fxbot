"""运行时路径辅助函数。"""

from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    """源码运行定位所属项目，安装运行定位当前工作项目。"""
    package = package_root()
    if package.parent.name == "src" and (package.parent.parent / "pyproject.toml").is_file():
        return package.parent.parent
    return Path.cwd().resolve()


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def data_dir(name: str | None = None) -> Path:
    """返回项目下的数据目录，禁止子目录逃逸。"""
    return _directory(project_root() / "data", name)


def _directory(root: Path, name: str | None) -> Path:
    """建立指定根目录内的运行时子目录。"""
    root = root.resolve()
    child = (root / name).resolve() if name is not None else root
    if not child.is_relative_to(root):
        raise ValueError("运行时子目录不能超出数据目录")
    child.mkdir(parents=True, exist_ok=True)
    return child


def config_dir(name: str | None = None) -> Path:
    """返回项目数据目录中的配置目录。"""
    return _directory(data_dir("config"), name)


def cache_dir(name: str | None = None) -> Path:
    """返回项目数据目录中的缓存目录。"""
    return _directory(data_dir("cache"), name)


def database_path() -> Path:
    return data_dir("db") / "fxbot.db"


def startup_marker_path() -> Path:
    """当前进程的启动记录，每次启动（含 execv 重启）重写。"""
    return data_dir("system") / "startup.json"


def built_in_plugins_dir() -> Path:
    return package_root() / "plugins"
