"""下载、时区与图片来源辅助。"""

from __future__ import annotations

import base64
import asyncio
import binascii
from datetime import datetime

import httpx
from loguru import logger

from ...utils.tz import as_shanghai


class NetworkError(Exception):
    pass


async def download_url(url: str) -> bytes:
    """按旧行为重试下载，全部失败后抛出错误。"""
    async with httpx.AsyncClient() as client:
        for index in range(3):
            try:
                resp = await client.get(url, timeout=20)
                resp.raise_for_status()
                return resp.content
            except Exception as error:
                logger.warning(f"Error downloading {url}, retry {index}/3: {error}")
                await asyncio.sleep(3)
    raise NetworkError(f"{url} 下载失败！")


async def fetch_image_source(source: str | bytes) -> bytes | None:
    """读取字节、base64、data URL 或 HTTP(S) 图片，不读取消息提供的本地路径。"""
    if isinstance(source, bytes):
        return source
    if not isinstance(source, str) or not source:
        return None
    if source.startswith("base64://"):
        try:
            return base64.b64decode(source[len("base64://") :])
        except (binascii.Error, ValueError):
            return None
    if source.startswith("data:"):
        try:
            encoded = source.split(",", 1)[1]
            return base64.b64decode(encoded)
        except (IndexError, binascii.Error, ValueError):
            return None
    if not source.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(source, timeout=20, follow_redirects=True)
            response.raise_for_status()
            return response.content
    except httpx.HTTPError:
        return None


def add_timezone(dt: datetime) -> datetime:
    """添加时区，归一到北京时间用于展示与分桶。"""
    return as_shanghai(dt)
