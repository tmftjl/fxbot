"""视频解析下载器。"""

from __future__ import annotations

import uuid
import shutil
import asyncio
import hashlib
from pathlib import Path
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import httpx
from loguru import logger

from .types import VideoResult
from .config import cfg_general, cfg_network
from ...utils.http import ensure_public_http_url
from ...utils.paths import cache_dir

CACHE_DIR = cache_dir("video_parser")


class DownloadError(RuntimeError):
    """下载失败。"""


def _suffix_from_url(url: str, default: str) -> str:
    """从 URL 推断文件后缀。"""
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    return default


def create_download_dir() -> Path:
    """创建单次解析使用的临时下载目录。"""
    directory = CACHE_DIR / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def cleanup_download_dir(directory: Path) -> None:
    """清理单次解析留下的临时下载目录。"""
    resolved_cache = CACHE_DIR.resolve()
    resolved_directory = directory.resolve()
    if resolved_directory == resolved_cache or resolved_cache not in resolved_directory.parents:
        raise ValueError("不能清理本任务缓存目录以外的路径")
    if resolved_directory.exists():
        shutil.rmtree(resolved_directory)


def _cache_path(url: str, suffix: str, *, directory: Path) -> Path:
    """生成缓存路径。"""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return directory / f"{digest}{suffix}"


def _timeout() -> httpx.Timeout:
    """构造请求超时；大文件下载使用更长的读取窗口。"""
    seconds = float(cfg_general().get("request_timeout_seconds", 20))
    return httpx.Timeout(
        connect=min(15.0, seconds),
        read=max(240.0, seconds),
        write=10.0,
        pool=10.0,
    )


def _proxy() -> str | None:
    """读取代理地址。"""
    value = str(cfg_network().get("proxy") or "").strip()
    return value or None


async def download_file(
    url: str,
    *,
    suffix: str = ".mp4",
    headers: dict[str, str] | None = None,
    directory: Path | None = None,
    backup_urls: list[str] | None = None,
) -> Path:
    """下载远程文件，主地址失败时依次尝试备用地址。"""
    directory = directory or CACHE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    file_path = _cache_path(url, _suffix_from_url(url, suffix), directory=directory)
    if file_path.exists() and file_path.stat().st_size > 0:
        return file_path

    headers = headers or {}
    candidates = list(dict.fromkeys([url, *(backup_urls or [])]))
    last_error: BaseException | None = None
    for index, candidate in enumerate(candidates):
        try:
            return await _download_file_httpx(candidate, file_path=file_path, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            file_path.unlink(missing_ok=True)
            logger.warning(
                "[video_parser] httpx 下载失败，改用 curl_cffi 重试: host={} ({}/{}) {}: {}",
                urlsplit(candidate).netloc,
                index + 1,
                len(candidates),
                type(exc).__name__,
                exc,
            )
            try:
                return await _download_file_with_curl(candidate, file_path=file_path, headers=headers)
            except Exception as curl_exc:
                last_error = curl_exc
                file_path.unlink(missing_ok=True)
                logger.warning(
                    "[video_parser] curl_cffi 下载失败，切换备用地址: host={} ({}/{}) {}: {}",
                    urlsplit(candidate).netloc,
                    index + 1,
                    len(candidates),
                    type(curl_exc).__name__,
                    curl_exc,
                )
        except (DownloadError, ValueError) as exc:
            last_error = exc
            file_path.unlink(missing_ok=True)
            logger.warning(
                "[video_parser] 媒体地址不可用，切换备用地址: host={} ({}/{}) {}: {}",
                urlsplit(candidate).netloc,
                index + 1,
                len(candidates),
                type(exc).__name__,
                exc,
            )
        except BaseException:
            file_path.unlink(missing_ok=True)
            raise
    raise DownloadError("媒体下载失败") from last_error


async def _download_file_httpx(url: str, *, file_path: Path, headers: dict[str, str]) -> Path:
    """使用 httpx 下载文件。"""
    max_bytes = int(cfg_general().get("max_file_mb", 80)) * 1024 * 1024
    total = 0
    async with httpx.AsyncClient(timeout=_timeout(), proxy=_proxy(), follow_redirects=False) as client:
        current = url
        for _ in range(4):
            await ensure_public_http_url(current)
            async with AsyncExitStack() as stack:
                response = await stack.enter_async_context(client.stream("GET", current, headers=headers))
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise DownloadError("媒体重定向缺少地址")
                    current = str(response.url.join(location))
                    continue
                response.raise_for_status()
                _check_content_length(response.headers.get("Content-Length"), max_bytes)
                with file_path.open("wb") as file:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        _check_total_size(total, max_bytes)
                        file.write(chunk)
                return _ensure_downloaded(file_path)
        raise DownloadError("媒体重定向次数超出限制")


async def _download_file_with_curl(url: str, *, file_path: Path, headers: dict[str, str]) -> Path:
    """httpx 传输失败后，用 curl_cffi 的浏览器指纹重试同一地址。"""
    try:
        import curl_cffi
    except Exception as exc:
        raise DownloadError("缺少 curl_cffi 依赖，无法进行浏览器传输兜底") from exc

    max_bytes = int(cfg_general().get("max_file_mb", 80)) * 1024 * 1024
    total = 0
    async with curl_cffi.AsyncSession(allow_redirects=True, impersonate="chrome") as session:
        response = await session.get(url, headers=headers, timeout=240, stream=True)
        response.raise_for_status()
        _check_content_length(response.headers.get("Content-Length"), max_bytes)
        with file_path.open("wb") as file:
            async for chunk in response.aiter_content(chunk_size=1024 * 1024):
                total += len(chunk)
                _check_total_size(total, max_bytes)
                file.write(chunk)
    return _ensure_downloaded(file_path)


def _check_content_length(content_length: str | None, max_bytes: int) -> None:
    """检查响应声明大小。"""
    if content_length and int(content_length) > max_bytes:
        current_mb = int(content_length) / 1024 / 1024
        max_mb = max_bytes / 1024 / 1024
        raise DownloadError(f"文件大小超出配置限制：上限 {max_mb:g} MB，当前 {current_mb:.1f} MB")


def _check_total_size(total: int, max_bytes: int) -> None:
    """检查已下载大小。"""
    if total > max_bytes:
        current_mb = total / 1024 / 1024
        max_mb = max_bytes / 1024 / 1024
        raise DownloadError(f"文件大小超出配置限制：上限 {max_mb:g} MB，当前 {current_mb:.1f} MB")


def _ensure_downloaded(file_path: Path) -> Path:
    """确认文件有效。"""
    if file_path.stat().st_size == 0:
        file_path.unlink(missing_ok=True)
        raise DownloadError("文件为空")
    return file_path


async def _merge_av(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    """使用 ffmpeg 合并音视频。"""
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise DownloadError("服务器未安装 FFmpeg，暂时无法处理该视频")
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c",
            "copy",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise DownloadError("服务器未安装 FFmpeg，暂时无法处理该视频") from exc
    try:
        code = await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if code != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        raise DownloadError("ffmpeg 合并音视频失败")
    return output_path


async def download_video(result: VideoResult, *, directory: Path | None = None) -> Path:
    """下载解析结果中的视频。"""
    if not result.video_url:
        raise DownloadError("解析结果没有视频直链")
    max_duration = float(cfg_general().get("max_duration_seconds", 600))
    if result.duration and result.duration > max_duration:
        raise DownloadError(f"视频时长超出配置限制：上限 {max_duration:g} 秒，当前 {result.duration:g} 秒")

    if not result.audio_url:
        return await download_file(
            result.video_url,
            suffix=".mp4",
            headers=result.headers,
            directory=directory,
            backup_urls=result.video_backup_urls,
        )

    downloads = await asyncio.gather(
        download_file(
            result.video_url,
            suffix=".m4s",
            headers=result.headers,
            directory=directory,
            backup_urls=result.video_backup_urls,
        ),
        download_file(
            result.audio_url,
            suffix=".m4a",
            headers=result.headers,
            directory=directory,
            backup_urls=result.audio_backup_urls,
        ),
        return_exceptions=True,
    )
    for result_or_error in downloads:
        if isinstance(result_or_error, BaseException):
            raise result_or_error
    video_path, audio_path = downloads
    output_dir = directory or CACHE_DIR
    output = output_dir / f"{hashlib.sha1((result.video_url + result.audio_url).encode('utf-8')).hexdigest()}.mp4"
    return await _merge_av(video_path, audio_path, output)


async def download_images(result: VideoResult, *, directory: Path | None = None) -> list[Path]:
    """下载解析结果中的图片。"""
    if not result.image_urls:
        raise DownloadError("解析结果没有图片")
    tasks = [
        download_file(url, suffix=".jpg", headers=result.headers, directory=directory) for url in result.image_urls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    paths: list[Path] = []
    failures = 0
    for result_or_error in results:
        if isinstance(result_or_error, BaseException):
            failures += 1
            logger.opt(exception=result_or_error).warning("[video_parser] 图片下载失败，已跳过")
            continue
        paths.append(result_or_error)
    if failures:
        logger.warning("[video_parser] 图片下载完成，失败 {} 张，成功 {} 张", failures, len(paths))
    if not paths:
        raise DownloadError("图片下载失败")
    return paths
