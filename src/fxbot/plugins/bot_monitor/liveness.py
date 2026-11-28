"""宿主机容器发现与重启，供 Bot 监控定位账号对应的 NapCat 容器。"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

DISCOVERY_TTL = 600.0
"""账号与容器对应关系的缓存时长。"""

CONFIG_DIR = "/app/napcat/config"
"""容器内 OneBot 配置目录，用于从 onebot11_<账号>.json 反查容器。"""


async def _run(*args: str) -> tuple[int, str]:
    """执行宿主机命令并返回退出码与标准输出，调用方自行判定失败。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, NotImplementedError):
        return 127, ""
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode("utf-8", "ignore")


async def discover_containers(state: dict[str, Any], moment: float) -> dict[str, str]:
    """扫描运行中的容器，按 onebot11_<账号>.json 建立账号到容器的映射。"""
    cached = state.get("containers")
    if isinstance(cached, dict) and moment - float(state.get("discovered_at") or 0.0) < DISCOVERY_TTL:
        return cached

    mapping: dict[str, str] = {}
    code, output = await _run("docker", "ps", "--format", "{{.Names}}")
    if code == 0:
        for name in output.split():
            code, files = await _run("docker", "exec", name, "sh", "-c", f"ls {CONFIG_DIR} 2>/dev/null")
            if code != 0:
                continue
            for entry in files.split():
                if entry.startswith("onebot11_") and entry.endswith(".json"):
                    mapping.setdefault(entry[len("onebot11_") : -len(".json")], name)
    state["containers"] = mapping
    state["discovered_at"] = moment
    return mapping


async def restart_container(name: str) -> bool:
    """重启指定容器，返回是否成功。"""
    code, _ = await _run("docker", "restart", name)
    if code != 0:
        logger.warning("[BotMonitor] 重启容器 {} 失败（退出码 {}）", name, code)
        return False
    return True
