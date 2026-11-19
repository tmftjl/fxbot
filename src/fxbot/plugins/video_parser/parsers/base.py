"""视频平台解析调度。"""

from __future__ import annotations

import re
import html
from urllib.parse import unquote, urlsplit

from ..types import VideoResult
from ..config import cfg_platforms

URL_RE = re.compile(
    r"https?://[^\s<>\"']+|"
    r"(?:v\.douyin|jx\.douyin|jingxuan\.douyin|v\.kuaishou)\.com/[A-Za-z0-9._?%&+=/#@-]+|"
    r"xhslink\.(?:com|cn)/[A-Za-z0-9._?%&+=/#@-]+|"
    r"(?:v\.m\.)?chenzhongtech\.com/fw/[A-Za-z0-9._?%&+=/#@-]+|"
    r"mapp\.api\.weibo\.cn/fx/[A-Za-z0-9]+\.html|"
    r"(?:b23\.tv|bili2233\.cn)/[A-Za-z0-9._?%&+=/#@-]+|"
    r"(?:[A-Za-z0-9-]+\.)*(?:bilibili\.com|weibo\.com|weibo\.cn|xiaohongshu\.com|"
    r"xhslink\.com|xhslink\.cn|douyin\.com|kuaishou\.com)/[A-Za-z0-9._?%&+=/#@-]+"
)
SUPPORTED_URL_MARKERS = (
    "douyin.com",
    "iesdouyin.com",
    "jingxuan.douyin.com",
    "kuaishou.com",
    "chenzhongtech.com",
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    "weibo.com",
    "weibo.cn",
    "mapp.api.weibo.cn",
    "card.weibo.com",
    "bilibili.com",
    "b23.tv",
    "bili2233.cn",
)


class ParseError(RuntimeError):
    """解析失败。"""


def find_url(text: str) -> str | None:
    """从文本中寻找候选链接。"""
    variants = _text_variants(text)
    for candidate in variants:
        for matched in URL_RE.finditer(candidate):
            url = matched.group(0).strip().rstrip("，。；;)")
            url = url if url.startswith("http") else f"https://{url}"
            if _is_supported_url(url):
                return url
    bv = re.search(r"\b(?P<bvid>BV[0-9A-Za-z]{10})\b(?:\s+(?P<page>\d{1,3}))?", text)
    if bv:
        page = bv.group("page")
        return f"https://www.bilibili.com/video/{bv.group('bvid')}?p={page}" if page else bv.group("bvid")
    av = re.search(r"\b(?P<avid>av\d{6,})\b(?:\s+(?P<page>\d{1,3}))?", text, re.IGNORECASE)
    if av:
        page = av.group("page")
        return f"https://www.bilibili.com/video/{av.group('avid')}?p={page}" if page else av.group("avid")
    return None


def _text_variants(text: str) -> tuple[str, ...]:
    """生成常见卡片转义文本变体。"""
    first = html.unescape(text).replace("\\/", "/")
    second = unquote(first)
    third = unquote(second)
    return tuple(dict.fromkeys((first, second, third)))


def _platform_for_url(url: str) -> str | None:
    if re.fullmatch(r"BV[0-9A-Za-z]{10}|av\d{6,}", url, re.I):
        return "bilibili"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    groups = {
        "douyin": ("douyin.com", "iesdouyin.com"),
        "kuaishou": ("kuaishou.com", "chenzhongtech.com"),
        "xiaohongshu": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
        "weibo": ("weibo.com", "weibo.cn"),
        "bilibili": ("bilibili.com", "b23.tv", "bili2233.cn"),
    }
    host = parsed.hostname
    for platform, hosts in groups.items():
        if any(host == domain or host.endswith("." + domain) for domain in hosts):
            return platform
    return None


def _is_supported_url(url: str) -> bool:
    return _platform_for_url(url) is not None


def can_parse_url(url: str) -> bool:
    return _match_enabled_platform(url) is not None


def _match_enabled_platform(url: str) -> str | None:
    platform = _platform_for_url(url)
    return platform if platform and cfg_platforms()[platform] else None


async def parse_url(url: str) -> VideoResult:
    """按平台解析链接。"""
    platform = _match_enabled_platform(url)

    if platform == "douyin":
        from .douyin import parse

        return await parse(url)
    if platform == "kuaishou":
        from .kuaishou import parse

        return await parse(url)
    if platform == "xiaohongshu":
        from .xiaohongshu import parse

        return await parse(url)
    if platform == "weibo":
        from .weibo import parse

        return await parse(url)
    if platform == "bilibili":
        from .bilibili import parse

        return await parse(url)

    raise ParseError("没有启用的平台能处理该链接")
