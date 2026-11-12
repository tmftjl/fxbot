"""显式插件注册，不隐式扫描或执行目录入口。"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from .application import Application


@dataclass(frozen=True, slots=True)
class Plugin:
    """用唯一名称和安装函数定义原生插件。"""

    name: str
    install: Callable[[Application], None]


def load_plugins(application: Application, plugins: tuple[Plugin, ...]) -> None:
    """启动前按序安装插件，重复或安装失败均阻止启动。"""
    if application.state != "new":
        raise RuntimeError("插件只能在启动前安装")
    names = [plugin.name for plugin in plugins]
    if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
        raise ValueError("插件名称必须非空且唯一")
    for plugin in plugins:
        plugin.install(application)
