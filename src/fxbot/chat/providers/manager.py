"""Provider 工厂、在途租约和显式资源回收。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import AsyncIterator

from loguru import logger

from .base import STTProvider, TTSProvider, BaseProvider, ChatProvider, EmbeddingProvider
from ...config import ConfigManager, get_manager
from .register import provider_registry, get_provider_class


@dataclass(slots=True)
class _ProviderLease:
    """一个实例的在途引用与唯一关闭任务。"""

    provider: BaseProvider
    references: int = 0
    retired: bool = False
    closing: asyncio.Task | None = None


class ProviderManager:
    """配置变化只退役旧实例，最后一个租约释放后才关闭。"""

    def __init__(self, configuration: ConfigManager) -> None:
        """显式持有配置与实例，不导入 SDK，也不共享隐藏单例。"""
        self.configuration = configuration
        self._providers: dict[str, BaseProvider] = {}
        self._leases: dict[int, _ProviderLease] = {}
        self._closing: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._shutdown: asyncio.Task | None = None

    async def start(self) -> None:
        """显式开启下一次应用生命周期，关闭失败的实例不能重新使用。"""
        if self._shutdown is None:
            return
        await asyncio.shield(self._shutdown)
        if self._leases or self._providers or self._closing:
            raise RuntimeError("Provider 资源尚未全部释放")
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._shutdown = None

    def _configs(self) -> dict[str, dict[str, Any]]:
        """读取当前有效配置，不保留业务配置快照。"""
        return self.configuration.get_system()["chat"]["providers"]

    def _resolve_config(self, name: str, capability: str) -> tuple[str, dict[str, Any]]:
        """显式指定的名称不存在时直接失败，不偷偷换用别的模型。"""
        configs = self._configs()
        selected = name or (self.configuration.get_system()["chat"]["provider"] if capability == "chat" else "")
        if selected:
            if selected not in configs or configs[selected]["provider_type"] != capability:
                raise ValueError("指定 Provider 不存在或能力不匹配")
            return selected, configs[selected]
        for candidate, config in configs.items():
            if config["provider_type"] == capability:
                return candidate, config
        raise ValueError(f"未配置 {capability} Provider")

    def _create_provider(self, name: str, config: dict[str, Any]) -> BaseProvider:
        """选中唯一实现后才加载 SDK，实例独占自己的配置快照。"""
        cls = get_provider_class(f"{config['type']}_{config['provider_type']}")
        return cls(provider_id=name, config=deepcopy(config))

    def _ensure_open(self) -> None:
        """停止后不能创建或借用新实例。"""
        if self._shutdown is not None:
            raise RuntimeError("Provider 管理器已停止")

    async def _get(self, name: str, capability: str, *, acquire: bool = False) -> _ProviderLease:
        """串行获取实例，重建前先验证配置与能力。"""
        async with self._lock:
            self._ensure_open()
            provider_name, config = self._resolve_config(name, capability)
            key = f"{capability}:{provider_name}"
            cached = self._providers.get(key)
            expected = {"chat": ChatProvider, "embedding": EmbeddingProvider, "stt": STTProvider, "tts": TTSProvider}[
                capability
            ]
            if cached is not None and cached.config == config:
                state = self._leases[id(cached)]
            else:
                provider = self._create_provider(provider_name, config)
                state = _ProviderLease(provider)
                self._leases[id(provider)] = state
                if not isinstance(provider, expected):
                    state.retired = True
                    self._close_retired(state)
                    raise TypeError("Provider 实现与声明能力不一致")
                self._providers[key] = provider
                if cached is not None:
                    retired = self._leases[id(cached)]
                    retired.retired = True
                    self._close_retired(retired)
            if acquire:
                state.references += 1
            return state

    def _close_retired(self, state: _ProviderLease) -> None:
        """资源尚有引用时不能关闭，每个实例只调度一次关闭。"""
        if not state.retired or state.references or state.closing is not None:
            return
        task = asyncio.create_task(state.provider.close(), name=f"provider.close:{state.provider.provider_id}")
        state.closing = task
        self._closing.add(task)
        task.add_done_callback(lambda finished: self._closed(state, finished))

    def _closed(self, state: _ProviderLease, task: asyncio.Task) -> None:
        """只在关闭成功后移除所有权；失败保留给显式关闭操作报告。"""
        if not task.cancelled():
            error = task.exception()
            if error is None:
                self._leases.pop(id(state.provider))
                self._closing.remove(task)
            else:
                logger.opt(exception=(type(error), error, error.__traceback__)).error("Provider 关闭失败")
        self._changed.set()

    async def _return(self, state: _ProviderLease) -> None:
        """归还在途引用并触发已退役实例的回收。"""
        async with self._lock:
            state.references -= 1
            self._close_retired(state)
            self._changed.set()

    @asynccontextmanager
    async def lease(self, name: str = "", *, capability: str = "chat") -> AsyncIterator[BaseProvider]:
        """租用对话实例，热更新和重置不能中断已借出的请求。"""
        state = await self._get(name, capability, acquire=True)
        try:
            yield state.provider
        finally:
            await self._return(state)

    @asynccontextmanager
    async def lease_snapshot(self, name: str, config: dict[str, Any]) -> AsyncIterator[ChatProvider]:
        """临时快照实例也登记资源所有权，退出时等待关闭。"""
        async with self._lock:
            self._ensure_open()
            provider = self._create_provider(name, config)
            state = _ProviderLease(provider, references=1, retired=True)
            self._leases[id(provider)] = state
        try:
            if not isinstance(provider, ChatProvider):
                raise TypeError("请求快照需要对话 Provider")
            yield provider
        finally:
            await self._return(state)
            await asyncio.shield(state.closing)

    async def get_provider(self, name: str = "") -> ChatProvider:
        """读取当前对话实例，跨异步请求持有时必须使用租约。"""
        return (await self._get(name, "chat")).provider

    async def get_embedding_provider(self, name: str = "") -> EmbeddingProvider:
        """读取嵌入实例。"""
        return (await self._get(name, "embedding")).provider

    async def get_stt_provider(self, name: str = "") -> STTProvider:
        """读取语音识别实例。"""
        return (await self._get(name, "stt")).provider

    async def get_tts_provider(self, name: str = "") -> TTSProvider:
        """读取语音合成实例。"""
        return (await self._get(name, "tts")).provider

    def _retire_all(self) -> None:
        """退役当前缓存，但不清除仍在使用或关闭失败的资源。"""
        for provider in self._providers.values():
            state = self._leases[id(provider)]
            state.retired = True
            self._close_retired(state)
        self._providers.clear()

    @staticmethod
    async def _drain(tasks: tuple[asyncio.Task, ...]) -> None:
        """所有关闭完成后再向上报告失败，不能中途丢下其他资源。"""
        results = await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def reset(self) -> None:
        """热重置缓存，已借出的实例会在归还时退役。"""
        async with self._lock:
            self._ensure_open()
            self._retire_all()
            closing = tuple(self._closing)
        await self._drain(closing)

    async def aclose(self) -> None:
        """等待在途租约并关闭全部资源，取消等待者不取消实际清理。"""
        if self._shutdown is None:
            self._shutdown = asyncio.create_task(self._close_all(), name="providers.shutdown")
        await asyncio.shield(self._shutdown)

    async def _close_all(self) -> None:
        """停止新增请求后等待所有租约归还。"""
        async with self._lock:
            self._retire_all()
        while True:
            async with self._lock:
                if not any(state.references for state in self._leases.values()):
                    closing = tuple(self._closing)
                    break
                self._changed.clear()
            await self._changed.wait()
        await self._drain(closing)

    def list_cached(self) -> dict[str, str]:
        """返回缓存实例的能力及名称。"""
        return {name: type(provider).__name__ for name, provider in self._providers.items()}

    def list_registered(self) -> dict[str, str]:
        """列出声明的类型，不加载 SDK。"""
        return {name: meta.display_name or name for name, meta in provider_registry.items()}

    def list_providers_by_type(self, provider_type: str = "chat") -> list[str]:
        """按能力筛选已配置的实例。"""
        return [name for name, config in self._configs().items() if config["provider_type"] == provider_type]

    def has_provider(self, provider_type: str = "chat") -> bool:
        """返回是否至少配置了一个该能力的 Provider。"""
        return bool(self.list_providers_by_type(provider_type))

    def get_api_config(self, name: str) -> dict[str, Any] | None:
        """读取最新实例配置。"""
        return self._configs().get(name)


provider_manager = ProviderManager(get_manager())
