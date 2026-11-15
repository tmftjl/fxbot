"""控制台访问命令，与会员业务及聊天内核独立装配。"""

from __future__ import annotations

from .auth import rotate_console_token
from ..core import SessionTarget, HandlerContext, on_regex
from ..config import ConfigManager
from ..core.matcher import bind_matchers
from ..core.application import Application
from ..runtime.settings import Settings


def install(application: Application, configuration: ConfigManager, settings: Settings) -> None:
    async def owner(ctx: HandlerContext) -> bool:
        event = ctx.event
        identity = SessionTarget(event.platform, event.bot_id, "private", event.user_id).key()
        return event.target.kind == "private" and identity in configuration.get_system()["system"]["superusers"]

    login = on_regex(r"^今汐登录$", name="login", display_name="控制台登录", permission=owner)

    @login.handle()
    async def respond(ctx: HandlerContext) -> None:
        token = rotate_console_token()
        await ctx.finish(f"控制台登录地址：http://{settings.host}:{settings.port}/fxbot?token={token}")

    bind_matchers(application, "console", (login.snapshot(),))
