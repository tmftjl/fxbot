"""帮助图片渲染；使用 Playwright 截图旧版 HTML 模板，失败时回退到 PIL。"""

from __future__ import annotations

import io
import html
import base64
from typing import Any
from pathlib import Path

from PIL import Image, ImageDraw
from loguru import logger

from .config import RES_DIR
from ...utils.fonts import draw_text_with_fallback, load_fallback_font_pair
from ...runtime.browser import render_html

_PAGE_TIMEOUT = 15.0


def _mime(path: Path) -> str:
    """根据文件后缀推断 MIME。"""
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".woff":
        return "font/woff"
    if suffix == ".ttf":
        return "font/ttf"
    return "application/octet-stream"


def _data_uri(path: Path, mime: str | None = None) -> str:
    """把资源文件转为 data URI。"""
    return f"data:{mime or _mime(path)};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _inline_css(icon: Path | None = None) -> str:
    """内联旧版帮助图 CSS 和字体资源。"""
    common_css = (RES_DIR / "common" / "common.css").read_text(encoding="utf-8")
    help_css = (RES_DIR / "help" / "index.css").read_text(encoding="utf-8")

    replacements = {
        "./font/FZB.woff": _data_uri(RES_DIR / "common" / "font" / "FZB.woff"),
        "./font/FZB.ttf": _data_uri(RES_DIR / "common" / "font" / "FZB.ttf"),
        "./font/NZBZ.woff": _data_uri(RES_DIR / "common" / "font" / "NZBZ.woff"),
        "./font/NZBZ.ttf": _data_uri(RES_DIR / "common" / "font" / "NZBZ.ttf"),
    }
    for old, new in replacements.items():
        common_css = common_css.replace(old, new)
    icon_path = icon if icon and icon.is_file() else RES_DIR / "help" / "icon.png"
    help_css = help_css.replace("icon.png", _data_uri(icon_path))
    return f"<style>{common_css}\n{help_css}</style>"


def _text(value: Any) -> str:
    """帮助配置中的文本按纯文本插入 HTML。"""
    return html.escape(str(value or ""), quote=False)


def _build_html(
    title: str,
    sub_title: str,
    groups: list[dict[str, Any]],
    col_count: int,
    background: Path,
    footer: str | None = None,
    icon: Path | None = None,
) -> str:
    """构造旧版帮助图 HTML。"""

    def icon_css(index: int) -> str:
        if not index:
            return "display:none"
        x = (index - 1) % 10
        y = (index - x - 1) // 10
        return f"background-position:-{x * 50}px -{y * 50}px"

    def render_group(group: dict[str, Any]) -> str:
        items = group.get("list", []) or []
        rows: list[str] = []
        step = max(col_count, 1)
        for i in range(0, len(items), step):
            chunk = items[i : i + step]
            cells: list[str] = []
            for item in chunk:
                css = icon_css(int(item.get("icon") or 0))
                cells.append(
                    f"<div class='td'><span class='help-icon' style='{css}'></span>"
                    f"<strong class='help-title'>{_text(item.get('title', ''))}</strong>"
                    f"<span class='help-desc'>{_text(item.get('desc', ''))}</span></div>"
                )
            while len(cells) < step:
                cells.append("<div class='td'></div>")
            rows.append(f"<div class='tr'>{''.join(cells)}</div>")
        table = f"<div class='help-table'>{''.join(rows)}</div>" if rows else ""
        return f"<div class='cont-box'><div class='help-group'>{_text(group.get('group', ''))}</div>{table}</div>"

    bg_uri = _data_uri(background)
    footer_text = _text(footer) if footer and str(footer).strip() else "Created by dggb | Rendered by Playwright"
    return f"""
    <!doctype html>
    <html>
      <head>
        <meta charset='utf-8'/>
        {_inline_css(icon)}
        <style>
          .container {{ background: url('{bg_uri}') center !important; background-size: cover !important; }}
          .help-table .td, .help-table .th {{ width: {100 / max(col_count, 1):.6f}% !important; }}
        </style>
      </head>
      <body>
        <div class='container'>
          <div class='info-box'>
            <div class='head-box'>
              <div class='title'>{_text(title)}</div>
              <div class='label'>{_text(sub_title)}</div>
            </div>
          </div>
          {"".join(render_group(group) for group in groups)}
          <div class='copyright'>{footer_text}</div>
        </div>
      </body>
    </html>
    """


def _fallback_image(title: str, sub_title: str, groups: list[dict[str, Any]]) -> bytes:
    """Playwright 不可用时输出纯 PIL 摘要图。"""
    fonts = load_fallback_font_pair(size=24)
    title_fonts = load_fallback_font_pair(size=42)
    lines = [title, sub_title, ""]
    for group in groups[:12]:
        lines.append(f"【{group.get('group', '')}】")
        for item in group.get("list") or []:
            lines.append(f"  {item.get('title', '')}  {item.get('desc', '')}")
    width = 1200
    height = max(320, 40 + len(lines) * 34 + 40)
    image = Image.new("RGB", (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(image)
    y = 30
    draw_text_with_fallback(draw, (40, y), title, *title_fonts, fill=(30, 30, 30))
    y += 60
    for line in lines[1:]:
        draw_text_with_fallback(draw, (40, y), line, *fonts, fill=(60, 60, 60))
        y += 34
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _default_background() -> Path:
    """读取旧版默认背景图。"""
    img_dir = RES_DIR / "help" / "imgs"
    if img_dir.is_dir():
        for path in sorted(img_dir.iterdir()):
            if path.is_file():
                return path
    return RES_DIR / "help" / "imgs" / "default.jpg"


async def render_help_image(
    title: str,
    sub_title: str,
    groups: list[dict[str, Any]],
    col_count: int = 3,
    scale: float = 1.2,
    footer: str | None = None,
    background: Path | None = None,
    icon: Path | None = None,
) -> bytes:
    """按旧版 HTML/CSS 排版用 Chromium 截图，失败时回退到 PIL。"""
    bg = background if background and background.is_file() else _default_background()
    html_text = _build_html(title, sub_title, groups, max(col_count, 1), bg, footer, icon)
    try:
        return await render_html(
            html_text,
            selector=".container",
            scale=scale,
            load_timeout=_PAGE_TIMEOUT,
            screenshot_timeout=_PAGE_TIMEOUT,
            full_page=True,
        )
    except Exception as exc:
        logger.warning(f"[help] HTML 渲染失败，回退到 PIL: {type(exc).__name__}: {exc}")
        return _fallback_image(title, sub_title, groups)
