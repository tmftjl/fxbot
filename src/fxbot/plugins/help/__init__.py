"""内置帮助插件。"""

from __future__ import annotations

import io
from typing import Any
from pathlib import Path

from PIL import Image, ImageDraw

from ...core import Message, MessageSegment
from .config import HelpConfigRef, load_help_config, resolve_help_config
from ...core.plugin import PluginMetadata, on_regex
from ...utils.fonts import draw_text_with_fallback, load_fallback_font_pair
from ...utils.paths import cache_dir

try:
    from .renderer import render_help_image
except Exception:  # pragma: no cover - 渲染依赖异常时保留纯文本兜底
    render_help_image = None

HELP_PATTERN = r"^(?:#|＃|/)(.*?)帮助$"

__plugin_meta__ = PluginMetadata(
    name="帮助",
    description="根据关键词发送内置或子插件的帮助图。",
    usage="#帮助；#群管帮助；#B站直播帮助",
)

help_cmd = on_regex(
    HELP_PATTERN,
    name="help",
    display_name="帮助",
    level="all",
    scene="all",
    priority=5,
    block=True,
)


def _resolve_config_ref(keyword: str | None) -> HelpConfigRef | None:
    """解析帮助图配置。"""
    return resolve_help_config(keyword)


def _asset_path(config: dict[str, Any], field: str) -> Path | None:
    """解析帮助配置中的资源路径。"""
    value = str(config.get(field) or "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = Path(str(config.get("_base_dir") or "")) / path
    return path if path.is_file() else None


def _fallback_image(title: str, sub_title: str, groups_data: list[dict[str, Any]]) -> bytes:
    """帮助图纯文本兜底渲染。"""
    text = f"{title}\n{sub_title}\n\n" + "\n".join(
        f"【{group.get('group', '')}】 " + ", ".join(str(item.get("title", "")) for item in (group.get("list") or []))
        for group in groups_data
    )
    lines = text.split("\n")
    fonts = load_fallback_font_pair(size=24)
    width = max(480, max((len(line) for line in lines), default=20) * 14 + 40)
    height = max(320, 30 + len(lines) * 32 + 30)
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    y = 20
    for line in lines:
        draw_text_with_fallback(draw, (20, y), line, *fonts, fill=(32, 32, 32))
        y += 32

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _cache_file(config: dict[str, Any]) -> Path:
    """帮助图缓存写入运行数据目录，不写包目录。"""
    config_key = str(config.get("_config_key") or "help")
    return cache_dir("help") / f"{config_key}.png"


def _cache_is_valid(cache_file: Path, config: dict[str, Any], assets: list[Path]) -> bool:
    """配置、代码或资源更新后重新渲染。"""
    try:
        code_files = [
            Path(__file__),
            Path(__file__).parent / "renderer.py",
            Path(__file__).parent / "config.py",
            Path(__file__).parents[2] / "utils" / "fonts.py",
        ]
        config_file = Path(str(config.get("_config_path") or ""))
        mtimes = [path.stat().st_mtime for path in [*code_files, config_file, *assets] if path.is_file()]
        return cache_file.is_file() and cache_file.stat().st_mtime >= max(mtimes, default=0)
    except Exception:
        return False


@help_cmd.handle()
async def _handle_help(context) -> None:
    """发送对应关键词的帮助图。"""
    groups = context.match.groups
    keyword = str(groups[0]).strip() if groups and groups[0] else ""
    resolved_ref = _resolve_config_ref(keyword or None)
    if keyword and resolved_ref is None:
        return

    config = load_help_config(resolved_ref)
    title = str(config.get("title") or "帮助")
    sub_title = str(config.get("sub_title") or keyword)
    footer = config.get("footer")
    col_count = int(config.get("col_count", 3) or 3)
    groups_data = config.get("groups", []) or []
    background = _asset_path(config, "background")
    icon = _asset_path(config, "icon")

    cache_file = _cache_file(config)
    assets = [path for path in (background, icon) if path is not None]
    image_bytes = b""
    if _cache_is_valid(cache_file, config, assets):
        try:
            image_bytes = cache_file.read_bytes()
        except OSError:
            image_bytes = b""

    if not image_bytes and render_help_image is not None:
        try:
            image_bytes = await render_help_image(
                title=title,
                sub_title=sub_title,
                groups=groups_data,
                col_count=col_count,
                footer=str(footer) if footer is not None else None,
                background=background,
                icon=icon,
            )
        except Exception:
            image_bytes = b""
        if image_bytes:
            try:
                cache_file.write_bytes(image_bytes)
            except OSError:
                pass

    if not image_bytes:
        image_bytes = _fallback_image(title, sub_title, groups_data)

    await context.finish(Message([MessageSegment.image(image_bytes)]))
