"""独立应用装配入口。"""

from __future__ import annotations

from fxbot.core.plugins import Plugin, load_plugins
from fxbot.core.application import Application

from .runtime.commands import install


def create_application(*, plugins: tuple[Plugin, ...] = ()) -> Application:
    """先安装基础命令，再安装显式提供的原生插件。"""
    application = Application()
    load_plugins(application, (Plugin("runtime", install), *plugins))
    return application
