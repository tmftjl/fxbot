"""A_Memorix 内核在 FxBot 里的生命周期。"""

from __future__ import annotations

import asyncio

from loguru import logger

from .paths import package_root
from ..work_schedule import background_model_allowed
from ._host.host_config import load_amemorix_config
from .core.runtime.sdk_memory_kernel import SDKMemoryKernel


class AMemorixRuntime:
    def __init__(self) -> None:
        self._kernel: SDKMemoryKernel | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._error = ""
        self._stopping = False
        self._suspended = False

    @property
    def available(self) -> bool:
        return self._kernel is not None and not self._stopping and not self._error

    @property
    def suspended(self) -> bool:
        """休息时间整个内核停用，任何路径都不会再拉起后台循环。"""
        return self._suspended

    @property
    def error(self) -> str:
        return self._error

    def config(self):
        return load_amemorix_config()

    async def start(self) -> bool:
        """按配置启动内核；关闭或启动失败都只降级记忆。"""
        async with self._lock:
            if self._suspended:
                return False
            if self._started:
                return self.available
            cfg = load_amemorix_config()
            if not cfg.plugin.enabled:
                self._started = True
                logger.info("[A_Memorix] 未启用，跳过长期记忆内核")
                return False
            if not background_model_allowed():
                self._suspended = True
                logger.info("[A_Memorix] 休息时间，长期记忆内核暂不启动")
                return False
            self._started = True
            try:
                kernel = SDKMemoryKernel(plugin_root=package_root(), config=cfg.model_dump())
                await kernel.initialize()
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                logger.opt(exception=True).error("[A_Memorix] 内核启动失败，记忆功能降级")
                return False
            self._kernel = kernel
            self._error = ""
            logger.info("[A_Memorix] 内核已启动")
            return True

    async def get_kernel(self) -> SDKMemoryKernel | None:
        if self._suspended or self._stopping or self._error:
            return None
        if self._kernel is None and not self._started:
            await self.start()
        return self._kernel

    async def suspend(self) -> None:
        """进入休息时间：停掉内核，嵌入探测和所有后台维护随之停止。"""
        async with self._lock:
            if self._suspended and self._kernel is None:
                return
            self._suspended = True
        if self._kernel is not None:
            logger.info("[A_Memorix] 休息时间，暂停长期记忆内核")
        await self.stop()

    async def resume(self) -> None:
        """回到上班时间：恢复内核并按需补做积压的写入任务。"""
        async with self._lock:
            if not self._suspended:
                return
            self._suspended = False
        if not await self.start():
            return
        logger.info("[A_Memorix] 上班时间，长期记忆内核已恢复")

    async def sync_schedule(self) -> None:
        """按当前作息切换内核；调度器每 30 秒调用一次。"""
        if background_model_allowed():
            await self.resume()
        else:
            await self.suspend()

    async def stop(self) -> None:
        async with self._lock:
            kernel = self._kernel
            if kernel is None:
                self._started = False
                self._error = ""
                return
            self._stopping = True
            self._error = "长期记忆内核关闭尚未完成"
            try:
                await kernel.shutdown()
                self._kernel = None
                self._started = False
                self._error = ""
            finally:
                self._stopping = False


amemorix_runtime = AMemorixRuntime()
