"""今日运势图片。"""

from __future__ import annotations

import io
import json
import math
import random
from typing import Any
from pathlib import Path

import httpx
from PIL import Image, ImageOps, ImageDraw, UnidentifiedImageError

from .config import cfg_api_urls
from ...utils.tz import today_str
from ...core.types import Message, MessageSegment
from ...utils.http import get_shared_async_client
from ...core.plugin import on_regex
from ...core.router import HandlerContext
from ...utils.fonts import load_font, get_shared_font_path
from ...utils.paths import data_dir
from ...utils.json_file import read_json, write_json

RESOURCE_DIR = Path(__file__).parent / "resource"
DATA_DIR = data_dir("entertain")
JRYS_DEFS_FILE = RESOURCE_DIR / "jrys_data.json"
USER_DATA_FILE = DATA_DIR / "user_fortunes.json"

_JRYS_DATA: list[dict[str, Any]] = []
_USER_FORTUNES: dict[str, dict[str, Any]] = {}

_SHARED_FONT = get_shared_font_path()
FONT_MAIN = load_font(_SHARED_FONT, 48)
FONT_LARGE = load_font(_SHARED_FONT, 90)
FONT_MEDIUM = load_font(_SHARED_FONT, 32)
FONT_SMALL = load_font(_SHARED_FONT, 26)
FONT_TINY = load_font(_SHARED_FONT, 22)


def _load_fortune_defs() -> None:
    """读取自带运势库，资源损坏直接报告。"""
    global _JRYS_DATA
    _JRYS_DATA = json.loads(JRYS_DEFS_FILE.read_text(encoding="utf-8"))
    if not isinstance(_JRYS_DATA, list) or not _JRYS_DATA or not all(isinstance(item, dict) for item in _JRYS_DATA):
        raise ValueError("运势库格式错误")


def _load_user_fortunes() -> None:
    global _USER_FORTUNES
    records = read_json(USER_DATA_FILE, dict)
    if not isinstance(records, dict):
        raise ValueError("运势记录必须为对象")
    for key, record in records.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(record, dict)
            or not isinstance(record.get("fortune"), dict)
            or not isinstance(record.get("time"), str)
        ):
            raise ValueError("运势记录需要用户 ID、运势及日期")
    _USER_FORTUNES = records


def _save_user_fortunes() -> None:
    write_json(USER_DATA_FILE, _USER_FORTUNES)


async def _get_background_image() -> Image.Image | None:
    """获取可选背景图。"""
    url = str(cfg_api_urls()["background_api"]).strip()
    if not url:
        return None
    try:
        client = await get_shared_async_client()
        response = await client.get(url)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGBA")
    except (httpx.HTTPError, UnidentifiedImageError, OSError):
        return None


def _sanitize_stars(text: str) -> str:
    """保留星级字符。"""
    return "".join(ch for ch in (text or "") if ch in {"★", "☆"})


def _draw_wrapped_text(text: str, max_chars: int) -> str:
    """按字符宽度换行。"""
    lines: list[str] = []
    for paragraph in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        buffer = ""
        for ch in paragraph:
            if len(buffer) >= max_chars:
                lines.append(buffer)
                buffer = ch
            else:
                buffer += ch
        if buffer:
            lines.append(buffer)
    return "\n".join(lines)


def _draw_star_rating(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    y: float,
    rating_string: str,
    star_size: int = 30,
    spacing: int = 10,
) -> None:
    """绘制旧版星级评分。"""
    stars = _sanitize_stars(rating_string)
    total_width = len(stars) * star_size + (len(stars) - 1) * spacing if stars else 0
    current_x = center_x - total_width / 2
    for ch in stars:
        cx = current_x + star_size / 2
        vertices = []
        for i in range(10):
            angle = math.pi / 5 * i - math.pi / 2
            radius = star_size / 2 if i % 2 == 0 else star_size / 4
            vertices.append((cx + radius * math.cos(angle), y + radius * math.sin(angle)))
        if ch == "★":
            draw.polygon(vertices, fill=(0, 0, 0, 220))
        else:
            draw.polygon(vertices, outline=(0, 0, 0, 220), width=2)
        current_x += star_size + spacing


def _generate_fortune_canvas(
    nickname: str,
    data: dict[str, Any],
    background: Image.Image | None = None,
) -> Image.Image:
    """生成旧版今日运势卡片。"""
    width, height = 650, 1000
    if background is None:
        image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    else:
        image = ImageOps.fit(background, (width, height), Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", (width, height), (255, 255, 255, 180))
    image = Image.alpha_composite(image.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(image)

    fortune = data.get("fortune", {})
    y = 80
    cx = width / 2
    color = (0, 0, 0, 220)

    draw.text((cx, y), f"{nickname}的今日运势", font=FONT_TINY, fill=color, anchor="mm")
    y += 80
    draw.text(
        (cx, y),
        fortune.get("fortuneSummary", "今日运势"),
        font=FONT_LARGE,
        fill=color,
        anchor="mm",
    )
    y += 120

    lucky_star = fortune.get("luckyStar", "")
    if lucky_star:
        _draw_star_rating(draw, cx, y, lucky_star)
    y += 100

    sign_text = fortune.get("signText", "")
    if sign_text:
        draw.text((cx, y), sign_text, font=FONT_MEDIUM, fill=color, anchor="mm")
        y += 80

    draw.line([(cx - 150, y), (cx + 150, y)], fill=(0, 0, 0, 100), width=2)
    y += 60

    wrapped = _draw_wrapped_text(fortune.get("unsignText", ""), 22)
    draw.multiline_text(
        (cx, y),
        wrapped,
        font=FONT_SMALL,
        fill=color,
        anchor="ma",
        spacing=15,
        align="center",
    )
    draw.text(
        (cx, height - 50),
        "| 仅供参考，切勿拘泥 |",
        font=FONT_TINY,
        fill=(0, 0, 0, 150),
        anchor="mm",
    )
    return image


def image_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _get_or_create_today_fortune(user_id: str) -> tuple[dict[str, Any], bool]:
    """获取或生成当天运势。"""
    if not _JRYS_DATA:
        _load_fortune_defs()
    today = today_str()
    _load_user_fortunes()
    record = _USER_FORTUNES.get(user_id)
    if record and record.get("time") == today:
        return record, False
    if not _JRYS_DATA:
        raise ValueError("运势库为空，无法生成")
    record = {"fortune": random.choice(_JRYS_DATA), "time": today}
    _USER_FORTUNES[user_id] = record
    # 生成后立即落盘，不依赖退出钩子。
    _save_user_fortunes()
    return record, True


handle_matcher = on_regex(r"^([#＃]|/)(?:今日运势|运势|抽签)", name="today", level="all")


@handle_matcher.handle()
async def handle(context: HandlerContext) -> None:
    event = context.event
    data, _ = _get_or_create_today_fortune(event.user_id)
    image = _generate_fortune_canvas(event.user_name or event.user_id, data, await _get_background_image())
    await context.send(Message([MessageSegment.image(image_bytes(image))]))
