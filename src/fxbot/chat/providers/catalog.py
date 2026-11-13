"""不导入 SDK 的内置 Provider 元数据目录。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from functools import partial
from importlib import import_module

from .entities import ProviderType, ProviderMetadata

if TYPE_CHECKING:
    from .base import BaseProvider

EMBED_BATCH_LIMIT = 25


def _load(module: str, name: str) -> type[BaseProvider]:
    """按确定的模块和类名加载，不捕获或降级依赖错误。"""
    return getattr(import_module(f"{__package__}.sources.{module}"), name)


def _definition(
    name: str,
    description: str,
    module: str,
    class_name: str,
    capability: ProviderType,
    defaults: dict,
) -> ProviderMetadata:
    """声明一次元数据与唯一工厂入口。"""
    return ProviderMetadata(
        id="default",
        model=None,
        type=name,
        desc=description,
        provider_type=capability,
        loader=partial(_load, module, class_name),
        default_config=defaults,
        display_name=name,
    )


BUILTIN_PROVIDERS = {
    item.type: item
    for item in (
        _definition(
            "openai_chat",
            "OpenAI / 兼容 API",
            "openai",
            "OpenAIProvider",
            ProviderType.CHAT,
            {"base_url": "https://api.openai.com/v1", "timeout": 60},
        ),
        _definition(
            "openai_embedding",
            "OpenAI Embedding",
            "openai",
            "OpenAIEmbeddingProvider",
            ProviderType.EMBEDDING,
            {
                "base_url": "https://api.openai.com/v1",
                "timeout": 60,
                "embedding_dim": 1536,
                "embed_batch_limit": EMBED_BATCH_LIMIT,
            },
        ),
        _definition(
            "openai_stt",
            "OpenAI 语音识别",
            "openai",
            "OpenAISTTProvider",
            ProviderType.STT,
            {"base_url": "https://api.openai.com/v1", "timeout": 60},
        ),
        _definition(
            "openai_tts",
            "OpenAI 语音合成",
            "openai",
            "OpenAITTSProvider",
            ProviderType.TTS,
            {"base_url": "https://api.openai.com/v1", "timeout": 60, "voice": "alloy"},
        ),
        _definition(
            "openai_responses_chat",
            "OpenAI Responses API",
            "openai_responses",
            "OpenAIResponsesProvider",
            ProviderType.CHAT,
            {"base_url": "https://api.openai.com/v1", "timeout": 60},
        ),
        _definition(
            "anthropic_chat",
            "Anthropic Claude API",
            "anthropic",
            "AnthropicProvider",
            ProviderType.CHAT,
            {"base_url": "https://api.anthropic.com", "timeout": 60},
        ),
        _definition(
            "gemini_chat",
            "Google Gemini API",
            "gemini",
            "GeminiProvider",
            ProviderType.CHAT,
            {"base_url": "https://generativelanguage.googleapis.com", "timeout": 60},
        ),
        _definition(
            "gemini_embedding",
            "Google Gemini Embedding",
            "gemini",
            "GeminiEmbeddingProvider",
            ProviderType.EMBEDDING,
            {"base_url": "https://generativelanguage.googleapis.com", "timeout": 60, "embedding_dim": 768},
        ),
        _definition(
            "vertex_chat",
            "Google Vertex AI REST API",
            "vertex",
            "VertexAIProvider",
            ProviderType.CHAT,
            {"base_url": "", "timeout": 60},
        ),
    )
}
