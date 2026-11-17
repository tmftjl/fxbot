"""插件依赖检查与批量安装；冲突只报告，由插件维护者自行解决。"""

from __future__ import annotations

import sys
import shutil
import importlib
import subprocess
from pathlib import Path
from importlib import metadata
from collections.abc import Iterable

from loguru import logger
from packaging.utils import canonicalize_name
from packaging.requirements import Requirement, InvalidRequirement

from .processes import terminate_process

_INSTALL_TIMEOUT_SECONDS = 180
_CLOSE_TIMEOUT_SECONDS = 5


def plugin_requirements(plugin_dir: Path) -> list[Requirement]:
    """读取插件 requirements.txt，忽略注释、空行和不适用于当前环境的标记。"""
    manifest = plugin_dir / "requirements.txt"
    if not manifest.is_file():
        return []
    result: list[Requirement] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            logger.warning("[plugin-deps] {} 的依赖声明无效，已跳过: {}", plugin_dir.name, line)
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        result.append(requirement)
    return result


def installed_versions() -> dict[str, str]:
    """返回已安装发行版名称到版本，名称按 PEP 503 规范化。"""
    versions: dict[str, str] = {}
    for distribution in metadata.distributions():
        name, version = distribution.metadata["Name"], distribution.version
        if isinstance(name, str) and name and version:
            versions[canonicalize_name(name)] = version
    return versions


def collect_missing(names: Iterable[str], plugins_root: Path) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """收集尚未安装的插件依赖，附带声明它们的插件名。"""
    installed = installed_versions()
    missing: dict[str, list[str]] = {}
    for name in names:
        for requirement in plugin_requirements(plugins_root / name):
            if canonicalize_name(requirement.name) not in installed:
                missing.setdefault(str(requirement), []).append(name)
    return list(missing), {key: tuple(value) for key, value in missing.items()}


def build_install_command() -> list[str]:
    """优先使用 uv，缺少 uv 时退回当前解释器的 pip。"""
    uv = shutil.which("uv")
    if uv is not None:
        return [uv, "pip", "install", "--python", sys.executable]
    return [sys.executable, "-m", "pip", "install"]


def install_packages(packages: list[str]) -> bool:
    """批量安装依赖；失败只记录冲突，不修改核心依赖也不阻止启动。"""
    if not packages:
        return True
    command = [*build_install_command(), *packages]
    logger.info("[plugin-deps] 安装插件依赖: {}", " ".join(packages))
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            stdin=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
        )
    except OSError as error:
        logger.error("[plugin-deps] 无法启动依赖安装命令: {}", error)
        return False
    try:
        try:
            stdout, stderr = process.communicate(timeout=_INSTALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            terminate_process(process)
            process.communicate(timeout=_CLOSE_TIMEOUT_SECONDS)
            logger.error("[plugin-deps] 插件依赖安装超时，进程已回收；核心继续启动，不自动重试")
            return False
    finally:
        if process.returncode is None:
            terminate_process(process)
            process.wait(timeout=_CLOSE_TIMEOUT_SECONDS)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if process.returncode != 0:
        output = stderr or stdout or ""
        if any(token in output for token in ("ResolutionImpossible", "conflicting", "Cannot install", "conflict")):
            logger.error(
                "[plugin-deps] 检测到插件依赖冲突，核心不替插件做版本仲裁；请由相关插件维护者解决。\n{}",
                output[-4000:],
            )
        else:
            logger.error(
                "[plugin-deps] 插件依赖安装失败，请由相关插件维护者解决；核心继续启动。\n{}",
                output[-4000:],
            )
        return False
    importlib.invalidate_caches()
    logger.info("[plugin-deps] 插件依赖安装完成")
    return True


def ensure_plugin_dependencies(names: Iterable[str], plugins_root: Path) -> None:
    """启动前补齐缺失的插件依赖；不升级已安装版本，冲突只报告。"""
    missing, owners = collect_missing(names, plugins_root)
    for requirement in missing:
        logger.info("[plugin-deps] {} 由插件 {} 声明", requirement, ", ".join(owners.get(requirement, ())))
    install_packages(missing)
