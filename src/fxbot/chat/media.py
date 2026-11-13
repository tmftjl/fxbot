"""模型侧图片读取、大小策略和压缩，不依赖消息框架。"""

from __future__ import annotations

import io
import base64
import asyncio
from typing import Any

import httpx
from PIL import Image, ImageOps
from loguru import logger

from ..config import get_manager as get_config_manager
from ..utils.http import ResponseTooLargeError, read_limited, ensure_public_http_url, get_shared_async_client

_MAX_IMAGE_SIDE = 512
_MAX_IMAGE_BYTES_SENT = 1024 * 1024
_MAX_REDIRECTS = 3
_IMAGE_SIDES = {"normal": 512, "high": 1024}
_DEFAULT_LIMIT = object()
_OVERSIZED_READ_BYTES = 32 * 1024 * 1024


def _visual() -> dict[str, Any]:
    """只消费配置边界已合并且校验通过的视觉设置。"""
    return get_config_manager().get_system()["chat"]["visual"]


def max_image_count() -> int:
    """单条消息最多处理的图片数；0 表示不接收图片。"""
    return _visual()["max_image_num"]


def _max_image_bytes() -> int | None:
    """图片大小上限；0 表示不限。"""
    value = _visual()["max_image_size_mb"]
    return int(value * 1024 * 1024) or None


def _max_image_side() -> int:
    """图片最长边；normal 512，high 1024。"""
    return _IMAGE_SIDES[_visual()["image_quality"]]


def _handle_oversized() -> bool:
    """过大图片是否处理；关闭时仍按大小上限直接跳过。"""
    return _visual()["handle_oversized_images"]


def _oversized_method() -> str:
    """过大图片处理方式：compress 或 discard。"""
    return _visual()["oversized_image_handle_method"]


def _download_limit(limit: int | None, *, compress_oversized: bool) -> int | None:
    """压缩模式下先按硬上限读入，再压到配置大小。"""
    if limit is None or not compress_oversized or not _handle_oversized() or _oversized_method() != "compress":
        return limit
    return max(limit, _OVERSIZED_READ_BYTES)


def _compress_to_limit(data: bytes, limit: int) -> bytes:
    """按画质和尺寸逐级压缩，不能解码的数据直接拒绝。"""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    image.load()
    if image.mode in {"RGBA", "LA", "P"}:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    original = image.copy()
    for side in (_MAX_IMAGE_SIDE, 1024, 768, 512, 384, 256, 160):
        candidate = original.copy()
        candidate.thumbnail((side, side))
        for quality in (85, 70, 55, 40):
            buffer = io.BytesIO()
            candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
            result = buffer.getvalue()
            if result and len(result) <= limit:
                return result
    return data


def _image_mime(data: bytes) -> str:
    """按文件头识别图片类型，供模型接口构造 data URL。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    raise ValueError("不支持此图片格式")


def _compress_image(data: bytes) -> bytes:
    """缩小过大的图片，无法解码时不伪装成有效图片。"""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    image.load()
    side = _max_image_side()
    if max(image.size) <= side and len(data) <= _MAX_IMAGE_BYTES_SENT:
        return data
    if image.mode in {"RGBA", "LA", "P"}:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((side, side))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85, optimize=True)
    return buffer.getvalue() or data


async def _fetch_remote_image(url: str, limit: int | None | object = _DEFAULT_LIMIT) -> bytes:
    """下载公网图片：逐跳校验重定向，流式读取并限制大小。"""
    if limit is _DEFAULT_LIMIT:
        limit = _max_image_bytes()
    client = await get_shared_async_client()
    for _ in range(_MAX_REDIRECTS + 1):
        await ensure_public_http_url(url)
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("重定向缺少 Location")
                url = str(response.url.join(location))
                continue
            response.raise_for_status()
            # 0 表示不限模型侧大小，但读取仍保留硬上限，避免超大响应撑爆内存
            return await read_limited(
                response.aiter_bytes(), response.headers.get("content-length"), limit or 32 * 1024 * 1024
            )
    raise ValueError("重定向次数过多")


async def _load_image(
    source: str | bytes,
    limit: int | None | object = _DEFAULT_LIMIT,
    *,
    compress_oversized: bool = True,
) -> bytes:
    """加载内联字节、图片 data URL、base64 或公网图片，拒绝本地路径。"""
    if limit is _DEFAULT_LIMIT:
        limit = _max_image_bytes()
    read_limit = _download_limit(limit, compress_oversized=compress_oversized)
    if isinstance(source, bytes):
        data = source
    else:
        value = str(source or "").strip()
        if value.startswith(("data:", "base64://")):
            if value.startswith("data:"):
                header, separator, encoded = value.partition(",")
                if not separator or not header.startswith("data:image/") or not header.endswith(";base64"):
                    raise ValueError("内联图片必须使用图片 MIME 类型与 base64 编码")
            else:
                encoded = value[9:]
            if read_limit is not None and len(encoded) > read_limit * 4 // 3 + 4:
                raise ResponseTooLargeError("base64 图片超过大小上限")
            data = base64.b64decode(encoded, validate=True)
        elif value:
            data = await _fetch_remote_image(value, limit=read_limit)
        else:
            raise ValueError("图片源为空")
    if limit is not None and len(data) > limit:
        if compress_oversized and _handle_oversized() and _oversized_method() == "compress":
            data = await asyncio.to_thread(_compress_to_limit, data, limit)
        if len(data) > limit:
            raise ResponseTooLargeError(f"图片超过大小上限：{len(data)} > {limit}")
    return data


async def load_image_bytes(source: str | bytes, *, limit: int | None | object = _DEFAULT_LIMIT) -> bytes:
    """按调用方上限加载图片原始字节；表情采集用自己的大小配置，不压缩。"""
    return await _load_image(source, limit=limit, compress_oversized=False)


async def load_images(sources: list[str | bytes]) -> list[str]:
    """把原始图片源加载为模型可消费的 data URL。"""

    images: list[str] = []
    seen: set[str | bytes] = set()
    limit = max_image_count()
    for source in sources:
        if len(images) >= limit:
            break
        key: str | bytes = source if isinstance(source, bytes) else str(source or "")
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            data = await _load_image(source)
            if data:
                data = await asyncio.to_thread(_compress_image, data)
                images.append(f"data:{_image_mime(data)};base64,{base64.b64encode(data).decode('ascii')}")
        except (OSError, ValueError, ResponseTooLargeError, httpx.HTTPError, Image.DecompressionBombError):
            logger.opt(exception=True).warning("图片资源无效或读取失败")
            continue
    return images
