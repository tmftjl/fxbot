"""独立应用命令行入口。"""

from __future__ import annotations

import os
import argparse

from .settings import Settings


def main() -> None:
    """读取连接环境配置并运行单进程服务。"""
    parser = argparse.ArgumentParser(description="FxBot 独立运行时")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bot-id", action="append", required=True, help="允许的 Bot ID，可重复传入")
    args = parser.parse_args()
    try:
        settings = Settings(
            token=os.environ.get("FXBOT_ACCESS_TOKEN", ""),
            bot_ids=frozenset(args.bot_id),
            host=args.host,
            port=args.port,
        )
    except ValueError:
        parser.error("配置无效：请设置 FXBOT_ACCESS_TOKEN、有效 Bot ID 和监听端口")

    import uvicorn

    from .server import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        ws_max_size=1024 * 1024,
        ws_max_queue=32,
        timeout_graceful_shutdown=10,
    )
