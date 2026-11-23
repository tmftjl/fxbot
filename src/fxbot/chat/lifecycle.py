"""聊天共享服务的显式启动、定时维护和有序关闭。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress

from loguru import logger

from . import recall, router, commands
from .audit import stop_audit_observers
from .emoji import emoji_cleanup_tick, emoji_reconcile_tick
from .focus import focus_mode_manager
from .memory import state
from ..config import ConfigManager
from .providers import provider_manager
from .scheduling import chat_scheduling
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

# 长期记忆内核要加载图存储、向量库并预热稀疏索引，约 7 秒；放到后台，不拖慢连接恢复
_MEMORY_START_STOP_WAIT_SECONDS = 10.0


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
        self._memory_task: asyncio.Task[bool] | None = None

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
        if self.configuration.get_system()["chat"].get("enabled", True) and not provider_manager.has_provider("chat"):
            logger.warning("[Chat] 未配置对话 Provider，AI 回复将被跳过；请在控制台「模型配置」中添加对话模型")
        resources.callback(state.mark_degraded, "聊天存储已停止")
        resources.push_async_callback(amemorix_runtime.stop)
        resources.push_async_callback(self._stop_memory)
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
        resources.push_async_callback(chat_scheduling.stop)
        chat_scheduling.start()
        await state.prepare_memory()
        # 内核就绪前 get_kernel 返回 None，聊天自动降级；初始化完再恢复记忆能力。
        self._memory_task = asyncio.create_task(self._start_memory(), name="chat:memory-kernel")
        await image_writeback.start()
        await chat_writeback.start()
        resources.callback(self.maintenance.stop)
        self.maintenance.configure()
        resources.callback(self.configuration.subscribe(self.maintenance.configure))

    async def _start_memory(self) -> bool:
        """后台初始化长期记忆内核；失败只降级，不影响聊天主链路。"""
        try:
            return await amemorix_runtime.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).warning("[Chat] 长期记忆内核初始化失败")
            return False

    async def _stop_memory(self) -> None:
        """先把后台初始化收尾，再交给 amemorix_runtime.stop 关闭内核。"""
        task, self._memory_task = self._memory_task, None
        if task is None:
            return
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_MEMORY_START_STOP_WAIT_SECONDS)
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            except asyncio.CancelledError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise
        if task.done() and not task.cancelled():
            task.exception()

    async def stop(self) -> None:
        """其余清理完成后再报告失败，不因单个资源出错跳过后续清理。"""
        await self.resources.aclose()
