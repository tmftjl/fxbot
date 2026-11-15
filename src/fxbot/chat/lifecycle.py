"""聊天共享服务的显式启动、定时维护和有序关闭。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from . import recall, router, commands
from .audit import stop_audit_observers
from .emoji import emoji_cleanup_tick, emoji_reconcile_tick
from .focus import focus_mode_manager
from .memory import state
from ..config import ConfigManager
from .providers import provider_manager
from ..utils.http import close_shared_async_client
from .continuation import reply_scheduler
from .memory.tasks import MemoryMaintenance
from .work_schedule import refresh_calendar
from .memory.cleanup import lifecycle_tick
from .memory.trimmed import trimmed_learning
from .tools.subagent import subagent_tasks
from ..core.scheduler import Scheduler
from .amemorix.runtime import amemorix_runtime
from ..core.application import Application
from .amemorix.writeback import chat_writeback
from .behavior.turn_scheduler import normal_scheduler
from .amemorix.image_writeback import image_writeback
from ..permission.authorization import Authorizer
from .amemorix._host.runtime_loop import set_main_loop


class ChatLifecycle:
    """保持进程共享实例，应用负责配对获取和释放资源。"""

    def __init__(self, application: Application, configuration: ConfigManager, scheduler: Scheduler) -> None:
        self.application = application
        self.configuration = configuration
        self.resources = AsyncExitStack()
        self.maintenance = MemoryMaintenance(
            configuration,
            scheduler,
            calendar=refresh_calendar,
            lifecycle=lifecycle_tick,
            emoji_reconcile=emoji_reconcile_tick,
            emoji_cleanup=emoji_cleanup_tick,
        )

    def install(self, authorizer: Authorizer) -> None:
        """处理器注册不启动数据库或模型请求。"""
        commands.install(self.application, authorizer)
        recall.install(self.application)
        router.install(self.application, authorizer)
        self.application.resource(self.start, self.stop)

    async def start(self) -> None:
        """数据库就绪后获取聊天资源；释放职责先登记，再执行获取。"""
        resources = self.resources
        set_main_loop(asyncio.get_running_loop())
        resources.callback(set_main_loop, None)
        resources.push_async_callback(close_shared_async_client)
        resources.push_async_callback(provider_manager.aclose)
        await provider_manager.start()
        resources.callback(state.mark_degraded, "聊天存储已停止")
        resources.push_async_callback(amemorix_runtime.stop)
        resources.push_async_callback(trimmed_learning.stop)
        trimmed_learning.start()
        resources.push_async_callback(image_writeback.stop)
        resources.push_async_callback(chat_writeback.stop)
        resources.push_async_callback(stop_audit_observers)
        resources.push_async_callback(subagent_tasks.cancel_all)
        subagent_tasks.open()
        resources.push_async_callback(focus_mode_manager.stop_all)
        focus_mode_manager.start()
        resources.push_async_callback(reply_scheduler.stop)
        reply_scheduler.start()
        resources.push_async_callback(normal_scheduler.stop)
        normal_scheduler.start()
        await state.prepare_memory()
        await amemorix_runtime.start()
        await image_writeback.start()
        await chat_writeback.start()
        resources.callback(self.maintenance.stop)
        self.maintenance.configure()
        resources.callback(self.configuration.subscribe(self.maintenance.configure))

    async def stop(self) -> None:
        """其余清理完成后再报告失败，不因单个资源出错跳过后续清理。"""
        await self.resources.aclose()
