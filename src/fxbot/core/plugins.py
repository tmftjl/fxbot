"""插件目录发现、元信息、权限与资源的统一管理。"""

from __future__ import annotations

import sys
import inspect
from importlib import import_module
from contextlib import AsyncExitStack
from dataclasses import dataclass
from collections.abc import Mapping
from importlib.resources import files

from .tasks import TaskContext, collect_schedules
from .commands import collect_handlers, register_declarations
from .services import Service
from .scheduler import Job, Scheduler
from .application import Application, ApplicationState


@dataclass(frozen=True, slots=True)
class PluginContext:
    application: Application
    scheduler: Scheduler
    authorizer: object
    notifications: object


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """插件中文展示信息，内部 ID 始终来自目录名。"""

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
    """声明同步出站消息转换，核心负责安装及移除。"""
    if not inspect.isfunction(handler) or inspect.iscoroutinefunction(handler):
        raise TypeError("出站转换必须为同步函数")
    handler.__fxbot_outgoing__ = True
    return handler


def discover_plugins() -> tuple[PluginDefinition, ...]:
    """固定发现 plugins 下直接子包，不维护插件清单或元数据文件。"""
    return tuple(
        PluginDefinition(directory.name, f"fxbot.plugins.{directory.name}")
        for directory in sorted(files("fxbot.plugins").iterdir(), key=lambda item: item.name)
        if directory.is_dir()
        and directory.name.isidentifier()
        and not directory.name.startswith("_")
        and directory.joinpath("__init__.py").is_file()
    )


def load_plugins(context: PluginContext, plugins: tuple[PluginDefinition, ...]) -> None:
    if context.application.state is not ApplicationState.NEW:
        raise RuntimeError("插件只能在启动前安装")
    if context.scheduler.application is not context.application:
        raise ValueError("调度器不属于当前应用")
    names = [plugin.name for plugin in plugins]
    if len(names) != len(set(names)):
        raise ValueError("插件名称必须唯一")
    for definition in plugins:
        _install(context, definition)


def _install(context: PluginContext, definition: PluginDefinition) -> None:
    root = import_module(definition.module)
    metadata = vars(root).get("__plugin_meta__")
    if not isinstance(metadata, PluginMetadata):
        raise TypeError(f"插件必须声明 __plugin_meta__: {definition.module}")
    modules = [
        module
        for name, module in sorted(sys.modules.items())
        if name == definition.module or name.startswith(definition.module + ".")
    ]
    declarations = collect_handlers(modules)
    schedules = collect_schedules(modules)
    task_names = [item.name for _, item in schedules]
    if len(task_names) != len(set(task_names)):
        raise ValueError(f"插件任务名称重复: {definition.name}")
    names = [d.name or h.__name__ for h, d in declarations]
    if len(names) != len(set(names)):
        raise ValueError(f"插件处理器名称重复: {definition.name}")
    services = {}
    stack = AsyncExitStack()
    task_contexts = []
    commands = register_declarations(
        context.application, context.authorizer, definition.name, declarations, services, context
    )
    context.application.plugin_metadata[definition.name] = metadata
    context.application.command_metadata[definition.name] = {
        d.name or h.__name__: d.display_name or d.name or h.__name__ for h, d in declarations
    }

    async def start():
        context.authorizer.declare_commands(definition.name, commands)
        for module in modules:
            for handler in vars(module).values():
                if (
                    inspect.isfunction(handler)
                    and handler.__module__ == module.__name__
                    and getattr(handler, "__fxbot_outgoing__", False)
                ):
                    stack.callback(context.application.add_message_transform(handler))
        for handler, declaration in schedules:
            task_context = TaskContext(context.notifications, {}, services, context)
            task_contexts.append(task_context)

            async def run(handler=handler, task_context=task_context):
                await handler(task_context)
                task_context.state["completed"] = True

            job_name = f"{definition.name}:{declaration.name}"

            def configure(declaration=declaration, task_context=task_context, job_name=job_name, run=run):
                schedule = declaration.plan(task_context) if declaration.plan is not None else declaration.interval
                if schedule is None:
                    context.scheduler.remove(job_name)
                else:
                    context.scheduler.set(Job(job_name, schedule, run, declaration.run_on_start))

            stack.push_async_callback(context.scheduler.remove_and_wait, job_name)
            configure()
            if declaration.watch_config:
                stack.callback(context.authorizer.configuration.subscribe(configure))

    async def stop():
        try:
            await stack.aclose()
        finally:
            async with AsyncExitStack() as cleanup:
                cleanup.callback(services.clear)
                for instance in services.values():
                    if isinstance(instance, Service):
                        cleanup.push_async_callback(instance.close)
                for task_context in task_contexts:
                    task_context.state.clear()
                task_contexts.clear()

    context.application.resource(start, stop)
