"""语音消息转写：调用 STT Provider，把结果作为正文的一部分交给后续流程。"""

from __future__ import annotations

import base64
import asyncio

import httpx
from loguru import logger

from fxbot.core.errors import ActionFailed

from ..core import Event, BotClient, Capability, MessageSegment, UnsupportedCapability
from ..config import get_manager as get_config_manager
from .providers import provider_manager
from ..utils.http import read_limited, ensure_public_http_url, get_shared_async_client
from .providers.usage import track_model_task_usage

MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 3
VOICE_DISABLED = "[语音消息]"
VOICE_FAILED = "[语音消息，转录失败]"


def asr_enabled() -> bool:
    """读取已校验的语音识别开关。"""
    return get_config_manager().get_system()["chat"]["voice"]["enable_asr"]


def tts_enabled() -> bool:
    """读取已校验的语音合成开关。"""
    return get_config_manager().get_system()["chat"]["voice"]["enable_tts"]


def _routing_name(key: str) -> str:
    """读取任务路由里的 Provider 名称。"""
    return get_config_manager().get_system()["chat"]["model_routing"][key]


def voice_routing_name() -> str:
    """返回任务路由里的语音识别 Provider 名称。"""
    return _routing_name("voice")


def tts_routing_name() -> str:
    """返回任务路由里的语音合成 Provider 名称。"""
    return _routing_name("tts")


def audio_segments(event: Event) -> list[MessageSegment]:
    """从规范事件中读取语音及音频段，不猜测平台字段。"""
    message = event.original_message if event.original_message is not None else event.message
    return [segment for segment in message.segments if segment.type in {"voice", "audio"}]


def _audio_filename(data: bytes) -> str:
    """按文件头猜扩展名；OpenAI 转写接口依靠文件名判断格式。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "voice.wav"
    if data[:4] == b"OggS":
        return "voice.ogg"
    if data[:4] == b"fLaC":
        return "voice.flac"
    if data[:3] == b"ID3" or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "voice.mp3"
    if data[4:8] == b"ftyp":
        return "voice.m4a"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "voice.webm"
    if data.startswith(b"#!AMR"):
        return "voice.amr"
    if data.startswith(b"#!SILK"):
        return "voice.silk"
    raise ValueError("不支持此语音文件格式")


async def _download_audio(url: str) -> bytes:
    """下载公网语音：逐跳校验重定向并限制大小。"""
    client = await get_shared_async_client()
    for _ in range(MAX_REDIRECTS + 1):
        await ensure_public_http_url(url)
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("重定向缺少 Location")
                url = str(response.url.join(location))
                continue
            response.raise_for_status()
            return await read_limited(response.aiter_bytes(), response.headers.get("content-length"), MAX_AUDIO_BYTES)
    raise ValueError("重定向次数过多")


async def _load_audio(source: str | bytes) -> bytes:
    """加载语音源；只接受内联字节、base64 和公网 http(s) URL，拒绝本地路径。"""
    if isinstance(source, bytes):
        data = source
    else:
        value = str(source or "").strip()
        if value.startswith("base64://"):
            data = base64.b64decode(value[9:], validate=True)
        elif value.startswith("data:") and ";base64," in value:
            data = base64.b64decode(value.split(";base64,", 1)[1], validate=True)
        elif value.startswith(("http://", "https://")):
            data = await _download_audio(value)
        else:
            raise ValueError("语音源不是可下载的 HTTP(S) 地址")
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError(f"语音超过 {MAX_AUDIO_BYTES // 1024 // 1024} MiB 上限")
    return data


async def transcribe(data: bytes) -> str | None:
    """通过租约识别语音，SDK 或实现错误直接交给调用边界。"""
    async with provider_manager.lease(voice_routing_name(), capability="stt") as provider:
        timeout = float(provider.config["timeout"])
        async with track_model_task_usage("voice", provider):
            text = await asyncio.wait_for(provider.transcribe(data, _audio_filename(data)), timeout=timeout)
    return text.strip() if text else None


async def synthesize(text: str) -> bytes | None:
    """通过租约生成语音，不在热重载时关闭在途客户端。"""
    text = text.strip()
    if not text:
        return None
    async with provider_manager.lease(tts_routing_name(), capability="tts") as provider:
        timeout = float(provider.config["timeout"])
        async with track_model_task_usage("tts", provider):
            audio = await asyncio.wait_for(provider.synthesize(text), timeout=timeout)
            return audio or None


async def transcribe_event(bot: BotClient, event: Event) -> str:
    """从平台客户端解析媒体引用，输入或传输失败返回固定标记。"""
    segments = audio_segments(event)
    if not segments:
        return ""
    if not asr_enabled():
        return VOICE_DISABLED
    try:
        source = await bot.capabilities.require(Capability.MEDIA).resolve_media(segments[0])
        data = await _load_audio(source)
        text = await transcribe(data)
    except (OSError, ValueError, asyncio.TimeoutError, httpx.HTTPError, UnsupportedCapability, ActionFailed):
        logger.opt(exception=True).warning("语音消息读取或处理失败")
        return VOICE_FAILED
    return f"[语音: {text}]" if text else VOICE_FAILED
