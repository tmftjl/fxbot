"""独立应用命令行入口。"""

from __future__ import annotations

import argparse
from dataclasses import replace

from .settings import Settings


def main() -> None:
    """读取项目配置并运行单进程服务。"""
    parser = argparse.ArgumentParser(description="FxBot 独立运行时")
    parser.add_argument("--host", help="覆盖项目配置中的监听地址")
    parser.add_argument("--port", type=int, help="覆盖项目配置中的监听端口")
    args = parser.parse_args()
    try:
        settings = Settings.load()
        overrides = {name: getattr(args, name) for name in ("host", "port") if getattr(args, name) is not None}
        settings = replace(settings, **overrides)
    except (OSError, ValueError):
        parser.error("无法读取有效的 data/config/runtime/config.json，请检查令牌和监听配置")

    import uvicorn

    from .server import create_app
    from .system_commands import reexec_if_pending

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        ws_max_size=1024 * 1024,
        ws_max_queue=32,
        timeout_graceful_shutdown=10,
    )
    reexec_if_pending()
