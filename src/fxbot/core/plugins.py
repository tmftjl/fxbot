"""插件定义发现与应用级运行资源装配。"""

from __future__ import annotations

import sys
import inspect
from types import ModuleType
from importlib import import_module
from contextlib import AsyncExitStack
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable
from importlib.resources import files

from loguru import logger

from .tasks import Scheduled, ScheduledTask, ScheduledHandler, collect_schedules
from .types import Message, BotClient, SessionTarget
from .matcher import Matcher, MatcherDefinition, bind_matchers
from .commands import Command
from .services import ServiceScope
from .contracts import Authorization, Configuration, Notifications
from .scheduler import Scheduler
from .application import Application, ApplicationState


@dataclass(frozen=True, slots=True)
class PluginContext:
    application: Application
    scheduler: Scheduler
    authorizer: Authorization
    notifications: Notifications
    configuration: Configuration


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """中文展示信息，内部 ID 来自目录名。"""

    name: str
    description: str = ""
    usage: str = ""
    type: str = "application"
    homepage: str = ""
    extra: Mapping[str, object] | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("插件展示名称不能为空")


@dataclass(frozen=True, slots=True)
class PluginDefinition:
    name: str
    module: str


def outgoing(handler):
    """声明同步出站消息转换，由应用负责安装与移除。"""
    if not inspect.isfunction(handler) or inspect.iscoroutinefunction(handler):
        raise TypeError("出站转换必须为同步函数")
    setattr(handler, "__fxbot_outgoing__", True)
    return handler


def discover_plugins() -> tuple[PluginDefinition, ...]:
    return tuple(
        PluginDefinition(directory.name, f"fxbot.plugins.{directory.name}")
        for directory in sorted(files("fxbot.plugins").iterdir(), key=lambda item: item.name)
        if directory.is_dir()
        and directory.name.isidentifier()
        and not directory.name.startswith("_")
        and directory.joinpath("__init__.py").is_file()
    )


def collect_matchers(modules: tuple[ModuleType, ...]) -> tuple[MatcherDefinition, ...]:
    """只收集本插件处理器所属的响应器，重导出不重复注册。"""
    module_names = {module.__name__ for module in modules}
    seen: set[Matcher] = set()
    result = []
    for module in modules:
        for value in vars(module).values():
            if not isinstance(value, Matcher) or value in seen:
                continue
            seen.add(value)
            definition = value.snapshot()
            if any(handler.callback.__module__ in module_names for handler in definition.handlers):
                result.append(definition)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    name: str
    metadata: PluginMetadata
    matchers: tuple[MatcherDefinition, ...]
    schedules: tuple[tuple[ScheduledHandler, Scheduled], ...]
    transforms: tuple[Callable[[BotClient, SessionTarget, Message], Message], ...]
    extensions: Mapping[str, Callable[[], object]] = field(default_factory=dict)

    @classmethod
    def read(cls, source: PluginDefinition) -> LoadedPlugin:
        root = import_module(source.module)
        metadata = vars(root).get("__plugin_meta__")
        if not isinstance(metadata, PluginMetadata):
            raise TypeError(f"插件必须声明 __plugin_meta__: {source.module}")
        modules = tuple(
            module
            for name, module in sorted(sys.modules.items())
            if name == source.module or name.startswith(source.module + ".")
        )
        matchers = collect_matchers(modules)
        schedules = collect_schedules(modules)
        for label, names in (
            ("响应器", [item.name for item in matchers]),
            ("任务", [item.name for _, item in schedules]),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"插件{label}名称重复: {source.name}")
        transforms = tuple(
            dict.fromkeys(
                handler
                for module in modules
                for handler in vars(module).values()
                if inspect.isfunction(handler)
                and handler.__module__ == module.__name__
                and getattr(handler, "__fxbot_outgoing__", False)
            )
        )
        extensions = vars(root).get("__plugin_extensions__", {})
        if not isinstance(extensions, dict) or any(
            not isinstance(name, str) or not name or not callable(callback) or inspect.iscoroutinefunction(callback)
            for name, callback in extensions.items()
        ):
            raise TypeError("插件扩展必须是名称到同步声明函数的映射")
        return cls(source.name, metadata, matchers, schedules, transforms, dict(extensions))


class PluginRuntime:
    """每个应用独立拥有服务、任务和订阅；插件模块只保存定义。"""

    def __init__(self, context: PluginContext, definition: LoadedPlugin):
        self.context = context
        self.definition = definition
        self.scope = ServiceScope()
        self.resources = AsyncExitStack()

    def install(self) -> None:
        application = self.context.application
        definition = self.definition
        if application.state is not ApplicationState.NEW:
            raise RuntimeError("响应器只能在应用启动前安装")
        application.router.validate_names(tuple(f"{definition.name}:{matcher.name}" for matcher in definition.matchers))
        bind_matchers(
            application,
            definition.name,
            definition.matchers,
            authorize=self.context.authorizer.check,
            scope=self.scope,
            app_services=self.context,
        )
        application.plugin_metadata[definition.name] = definition.metadata
        application.plugin_extensions[definition.name] = dict(definition.extensions)
        application.command_metadata[definition.name] = {item.name: item.display_name for item in definition.matchers}
        application.resource(self.start, self.stop)

    async def start(self) -> None:
        definition = self.definition
        self.context.authorizer.declare_commands(
            definition.name, tuple(Command(item.name, item.level, item.scene) for item in definition.matchers)
        )
        self.resources.push_async_callback(self.scope.close)
        for transform in definition.transforms:
            self.resources.callback(self.context.application.add_message_transform(transform))
        for handler, declaration in definition.schedules:
            task = ScheduledTask(
                f"{definition.name}:{declaration.name}", handler, declaration, self.scope, self.context
            )
            self.resources.callback(task.clear)
            self.resources.push_async_callback(self.context.scheduler.remove_and_wait, task.name)
            task.configure()
            if declaration.watch_config:
                self.resources.callback(self.context.configuration.subscribe(task.configure))

    async def stop(self) -> None:
        await self.resources.aclose()


def load_plugins(context: PluginContext, plugins: tuple[PluginDefinition, ...]) -> None:
    if context.application.state is not ApplicationState.NEW:
        raise RuntimeError("插件只能在启动前安装")
    if context.scheduler.application is not context.application:
        raise ValueError("调度器不属于当前应用")
    names = [plugin.name for plugin in plugins]
    if len(names) != len(set(names)):
        raise ValueError("插件名称必须唯一")
    loaded = []
    for source in plugins:
        try:
            from .registration import registration_scope

            with registration_scope():
                loaded.append(LoadedPlugin.read(source))
        except ImportError:
            # 插件缺少依赖属于插件自身的维护范围；核心记录后继续启动。
            logger.exception("插件 {} 依赖缺失或导入失败，已跳过；请由插件维护者解决依赖问题", source.name)
    definitions = tuple(loaded)
    context.application.router.validate_names(
        tuple(f"{plugin.name}:{matcher.name}" for plugin in definitions for matcher in plugin.matchers)
    )
    for definition in definitions:
        PluginRuntime(context, definition).install()
