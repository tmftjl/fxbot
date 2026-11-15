"""内置运行时连通性命令。"""

from __future__ import annotations

from fxbot.core import HandlerContext
from fxbot.core.matcher import Matcher, MatcherOptions, bind_matchers
from fxbot.core.matching import command_match
from fxbot.core.permission import Permission
from fxbot.core.application import Application

from ..permission.authorization import Authorizer


def install(application: Application, authorizer: Authorizer) -> None:
    """注册不访问数据库或模型的基础命令。"""

    async def ping(context: HandlerContext) -> None:
        """回复连通性检查。"""
        await context.finish("pong")

    matcher = Matcher(
        command_match("/ping", prefixes=("",)),
        MatcherOptions(
            name="ping", display_name="连通性检查", permission=Permission.for_event(authorizer.guard("runtime:ping"))
        ),
    )
    matcher.handle()(ping)
    bind_matchers(application, "runtime", (matcher.snapshot(),))
