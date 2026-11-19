"""快手视频解析。"""

from __future__ import annotations

from .base import ParseError
from ..types import VideoResult
from .common import IOS_HEADERS, first, get_text, final_url, extract_json

HEADERS = {**IOS_HEADERS, "Referer": "https://v.kuaishou.com/"}


async def parse(url: str) -> VideoResult:
    """解析快手视频。"""
    resolved = await final_url(url, headers=HEADERS)
    resolved = resolved.replace("/fw/long-video/", "/fw/photo/")
    html = await get_text(resolved, headers=HEADERS)
    data = extract_json(html, r"window\.INIT_STATE\s*=\s*(.*?)</script>")

    photo = None
    if isinstance(data, dict):
        for item in data.values():
            if isinstance(item, dict) and isinstance(item.get("photo"), dict):
                photo = item["photo"]
                break
    if photo is None:
        raise ParseError("快手页面没有视频信息")

    video_urls = _cdn_urls(photo.get("mainMvUrls"))
    image_urls = _atlas_urls(photo)
    if not video_urls and not image_urls:
        raise ParseError("快手页面没有可发送的媒体")

    title = str(photo.get("caption") or ("快手视频" if video_urls else "快手图集"))
    cover_urls = _cdn_urls(photo.get("coverUrls"))
    author = str(photo.get("userName") or "").replace("\u3164", "").strip()
    return VideoResult(
        platform="快手",
        title=title,
        video_url=video_urls[0] if video_urls else None,
        video_backup_urls=video_urls[1:],
        image_urls=image_urls,
        cover_url=cover_urls[-1] if cover_urls else (image_urls[0] if image_urls else None),
        duration=(float(photo.get("duration")) / 1000) if photo.get("duration") else None,
        source_url=resolved,
        author_name=author,
        text=title,
        headers=HEADERS.copy(),
    )


def _cdn_urls(items: object) -> list[str]:
    """提取快手 CDN 主地址和备用地址。"""
    if not isinstance(items, list):
        return []
    urls: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    return list(dict.fromkeys(urls))


def _atlas_urls(photo: dict[str, object]) -> list[str]:
    """提取快手图集图片。"""
    ext_params = photo.get("ext_params") or {}
    if not isinstance(ext_params, dict):
        return []
    atlas = ext_params.get("atlas") or {}
    if not isinstance(atlas, dict):
        return []
    cdn_items = atlas.get("cdnList") or []
    routes = atlas.get("list") or []
    cdn_item = first(cdn_items)
    if not isinstance(cdn_item, dict) or not isinstance(routes, list):
        return []
    cdn = str(cdn_item.get("cdn") or "").strip("/")
    if not cdn:
        return []
    return [f"https://{cdn}/{str(route).lstrip('/')}" for route in routes if route]
