"""洛克王国运行时资源下载。"""

from __future__ import annotations

import os
import re
import asyncio
import tempfile
from html import unescape
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx
from loguru import logger

from .config import resources_config
from ...utils.http import get_shared_async_client
from ...utils.paths import data_dir
from ...core.concurrency import run_concurrently

DEFAULT_URLS = [
    ("[CNJS]", "http://cn-js-nj-1.lcf.icu:13214"),
    ("[TW]", "http://tw-taipei-1.lcf.icu:20532"),
    ("[SG]", "http://sg-1.lcf.icu:12588"),
    ("[US]", "http://us-lax-2.lcf.icu:12588"),
    ("[Azure SG]", "https://sg-2.qxqx.cf"),
    ("[Oracle KR]", "https://kr.qxqx.cf"),
    ("[Oracle JP]", "https://jp.qxqx.cf"),
    ("[MiniGG]", "http://file.minigg.cn/sayu-bot"),
    ("[Lulu]", "http://lulush.microgg.cn"),
    ("[TakeyaYuki]", "https://gscore.focalors.com"),
    ("[Elysia]", "https://silverwing.elysia.beauty"),
]

_download_lock = asyncio.Lock()
_MAX_DEPTH = 16
_MAX_ENTRIES = 20_000
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"{kind}{i}" for kind in ("com", "lpt") for i in range(1, 10)}


def resource_dir() -> Path:
    base = data_dir("rocom")
    root = base / "resources"
    if root.is_symlink() or not root.resolve().is_relative_to(base.resolve()):
        raise ValueError("资源目录必须位于 rocom 数据目录内")
    return root


def _endpoints() -> dict[str, Path]:
    root = resource_dir()
    endpoints = {
        "resource/rocomicon": root / "rocomicon",
        "resource/skillicon": root / "skillicon",
        "resource/headicon": root / "headicon",
    }
    for target in endpoints.values():
        _checked_path(target, root.resolve())
    return endpoints


def resources_ready() -> bool:
    """检查运行时图标资源是否已存在。"""
    for target in _endpoints().values():
        if not target.is_dir():
            return False
        try:
            next(target.iterdir())
        except StopIteration:
            return False
    return True


async def ensure_rocom_resources(force: bool = False) -> None:
    """检查并下载 RocomUID 运行时资源。"""
    config = resources_config()
    if not config["enabled"]:
        return
    async with _download_lock:
        if resources_ready() and not force:
            return

        client = await get_shared_async_client()
        tag, base_url = await _choose_base_url(str(config.get("base_url") or ""))
        logger.info(f"[rocom] 使用资源站 {tag} {base_url} 下载运行时资源")
        total = 0
        concurrency = int(config.get("concurrency") or 12)
        for endpoint, target in _endpoints().items():
            changed = await _download_endpoint(client, base_url, endpoint, target, concurrency)
            total += changed
            logger.info(f"[rocom] 资源 {endpoint} 检查完成，更新 {changed} 个文件")
        logger.info(f"[rocom] 运行时资源检查完成，更新 {total} 个文件")


async def _choose_base_url(configured: str) -> tuple[str, str]:
    """选择可用资源站。"""
    client = await get_shared_async_client()
    candidates = [("[Config]", configured)] if configured else DEFAULT_URLS
    tasks = [asyncio.create_task(_probe(client, tag, url)) for tag, url in candidates if url]
    try:
        for task in asyncio.as_completed(tasks):
            result = await task
            if result is not None:
                return result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if configured:
        raise RuntimeError(f"配置资源站不可用：{configured}")
    raise RuntimeError("没有找到可用的 GsCore 资源站")


async def _probe(client, tag: str, base_url: str) -> tuple[str, str] | None:
    try:
        response = await client.get(base_url, timeout=8.0, follow_redirects=False)
        if response.status_code == 200 and "Index of /" in response.text:
            return tag, base_url.rstrip("/")
    except httpx.HTTPError:
        return None
    return None


def _checked_path(path: Path, root: Path) -> Path:
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("资源下载路径不能超出目标目录或指向符号链接")
    return path.resolve()


def _child_entry(url: str, target: Path, href: str, root: Path) -> tuple[str, Path, bool]:
    """索引只允许直接子项；URL 解码后再次验证跨平台文件名。"""
    link = urlsplit(unescape(href))
    if link.scheme or link.netloc or link.query or link.fragment:
        raise ValueError("资源索引不能引用绝对地址、查询参数或片段")
    name = unquote(link.path, errors="strict")
    directory = name.endswith("/")
    name = name[:-1] if directory else name
    device = name.split(".", 1)[0].rstrip(" ").casefold()
    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(character in '<>:"/\\|?*' or ord(character) < 32 for character in name)
        or device in _RESERVED_NAMES
    ):
        raise ValueError("资源索引包含无效的直接子项文件名")
    destination = _checked_path(target / name, root)
    return url + quote(name, safe="") + ("/" if directory else ""), destination, directory


async def _download_endpoint(client, base_url: str, endpoint: str, target: Path, concurrency: int) -> int:
    """同一目录树共享边界、并发限制和预算，下载任务不越过请求生命周期。"""
    if target.is_symlink():
        raise ValueError("资源根目录不能是符号链接")
    root = target.resolve()
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 32)))
    visited: dict[Path, bool] = {}

    async def visit(url: str, folder: Path, depth: int) -> int:
        if depth > _MAX_DEPTH:
            raise ValueError("资源索引超过目录深度预算")
        folder = _checked_path(folder, root)
        folder.mkdir(parents=True, exist_ok=True)
        response = await client.get(url, timeout=60.0, follow_redirects=False)
        response.raise_for_status()
        entries = []
        for href in _parse_links(response.text):
            if href == "../":
                continue
            entry = _child_entry(url, folder, href, root)
            if entry[1] in visited:
                if visited[entry[1]] != entry[2]:
                    raise ValueError("资源索引将同一路径同时声明为文件和目录")
                continue
            visited[entry[1]] = entry[2]
            if len(visited) > _MAX_ENTRIES:
                raise ValueError("资源索引超过条目预算")
            entries.append(entry)
        changed = 0
        for file_url, path, directory in entries:
            if directory:
                changed += await visit(file_url, path, depth + 1)
        changed += sum(
            await run_concurrently(
                _download_file(client, file_url, path, semaphore, root)
                for file_url, path, directory in entries
                if not directory
            )
        )
        return changed

    return await visit(f"{base_url.rstrip('/')}/RocomUID/{endpoint.strip('/')}/", root, 0)


def _parse_links(html: str) -> list[str]:
    """解析资源站目录页链接。"""
    return [
        item
        for item in re.findall(r"""<a\s+href=["']([^"']+)["']""", html, flags=re.I)
        if item and not item.startswith("?")
    ]


async def _download_file(client, url: str, path: Path, semaphore: asyncio.Semaphore, root: Path) -> int:
    async with semaphore:
        response = await client.get(url, timeout=120.0, follow_redirects=False)
        response.raise_for_status()
        content = response.content
        path = _checked_path(path, root)
        if path.exists() and os.stat(path).st_size == len(content):
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".download-", delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
            _checked_path(path, root)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return 1
