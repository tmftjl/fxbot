"""内置运行时连通性命令。"""

from __future__ import annotations

from fxbot.core import HandlerContext
from fxbot.core.application import Application

from ..permission.authorization import Authorizer


def install(application: Application, authorizer: Authorizer) -> None:
    """注册不访问数据库或模型的基础命令。"""

    async def ping(context: HandlerContext) -> None:
        """回复连通性检查。"""
        await context.finish("pong")

    application.router.command("ping", "/ping", ping, permission=authorizer.guard("runtime:ping"))
