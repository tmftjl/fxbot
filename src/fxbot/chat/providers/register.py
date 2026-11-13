"""Provider 声明目录、扩展注册和按需实现工厂。"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from .catalog import BUILTIN_PROVIDERS
from .entities import ProviderType, ProviderMetadata

if TYPE_CHECKING:
    from .base import BaseProvider

provider_registry: dict[str, ProviderMetadata] = dict(BUILTIN_PROVIDERS)


def register_provider(
    type_name: str,
    desc: str = "",
    provider_type: ProviderType = ProviderType.CHAT,
    default_config: dict[str, Any] | None = None,
    display_name: str | None = None,
):
    """Provider 注册装饰器。"""

    def decorator(cls: type["BaseProvider"]):
        if type_name in provider_registry:
            raise ValueError(f"Provider 类型重复注册: {type_name}")

        provider_registry[type_name] = ProviderMetadata(
            id="default",
            model=None,
            type=type_name,
            desc=desc,
            provider_type=provider_type,
            loader=lambda: cls,
            default_config=default_config,
            display_name=display_name or type_name,
        )
        return cls

    return decorator


def get_provider_class(type_name: str) -> type["BaseProvider"]:
    """获取已注册的 Provider 类。"""
    meta = provider_registry.get(type_name)
    if not meta:
        raise ValueError(f"未注册的 Provider 类型: {type_name}")
    return meta.cls_type


def list_providers() -> dict[str, ProviderMetadata]:
    """列出所有已注册的 Provider。"""
    return deepcopy(provider_registry)
