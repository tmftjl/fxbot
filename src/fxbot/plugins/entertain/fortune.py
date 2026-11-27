"""今日运势图片。"""

from __future__ import annotations

import io
import html
import json
import math
import time
import base64
import random
from typing import Any
from pathlib import Path
from datetime import date, datetime, timedelta

import httpx
from PIL import Image, ImageOps, ImageDraw, UnidentifiedImageError
from loguru import logger

from ...core import Capability, UnsupportedCapability
from .config import cfg_api_urls
from ...utils.tz import SHANGHAI_TZ, today, today_str
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


_TEMPLATE_PATH = RESOURCE_DIR / "fortune_card.html"
_FONT_FILES = (
    ("FortuneSans", RESOURCE_DIR / "fortune_sans.woff2"),
    ("FortuneKai", RESOURCE_DIR / "fortune_kai.woff2"),
)
_FONT_FACE_RULE: str | None = None
_DEFAULT_BG_PATH = RESOURCE_DIR / "fortune_bg.jpg"
_DEFAULT_BG: bytes | None = None
_TEMPLATE_CACHE: str | None = None
_GRADIENT_URI: str | None = None
_AVATAR_CACHE: dict[str, tuple[float, bytes]] = {}
_AVATAR_CACHE_SECONDS = 21600

# 按星级加权：中间档占大头，两端的极端签稀有。
_STAR_WEIGHTS = {0: 4, 1: 7, 2: 11, 3: 16, 4: 22, 5: 20, 6: 14, 7: 6}
_AXES = ("财运", "事业", "健康", "人缘", "爱情", "智慧")
_COLORS = (
    ("薄荷绿", "#8fd6a8"),
    ("樱粉", "#f4a7bb"),
    ("雾蓝", "#8fb8e6"),
    ("暖橙", "#f2b25c"),
    ("香芋紫", "#a98bd4"),
    ("奶白", "#f2ead8"),
    ("松石绿", "#5fbfae"),
    ("赤金", "#d4a33e"),
)
_DIRECTIONS = ("东", "东南", "南", "西南", "西", "西北", "北", "东北")
_WINDOWS = ("07:00-09:00", "09:00-11:00", "11:00-13:00", "15:00-17:00", "17:00-19:00", "21:00-23:00")
_GOOD = (
    ("抽卡", "entertain"),
    ("补番", "entertain"),
    ("开黑", "social"),
    ("逛谷", "shopping"),
    ("出片", "creative"),
    ("巡礼", "outdoor"),
    ("养肝", "rest"),
    ("二创", "creative"),
    ("看演出", "entertain"),
    ("吃好点", "food"),
    ("出门", "outdoor"),
    ("整理", "home"),
    ("早睡", "rest"),
    ("运动", "body"),
    ("学习", "study"),
    ("聊天", "social"),
    ("存钱", "finance"),
    ("散步", "outdoor"),
    ("拍照", "creative"),
    ("睡午觉", "rest"),
)
_BAD = (
    ("上头", "emotion"),
    ("氪金", "finance"),
    ("熬夜", "rest"),
    ("对线", "social"),
    ("硬聊", "social"),
    ("吃土", "finance"),
    ("立旗", "emotion"),
    ("拖延", "work"),
    ("内耗", "emotion"),
    ("连跪", "entertain"),
    ("深夜购", "shopping"),
    ("久坐", "body"),
    ("暴食", "food"),
    ("乱花", "finance"),
    ("通宵", "rest"),
    ("瞎买", "shopping"),
    ("较真", "social"),
    ("空想", "work"),
    ("摆烂", "work"),
    ("刷手机", "entertain"),
)


def _star_count(entry: dict[str, Any]) -> int:
    if "stars" in entry:
        try:
            return max(0, min(7, int(entry["stars"])))
        except (TypeError, ValueError):
            return 0
    return _sanitize_stars(str(entry.get("luckyStar", ""))).count("★")


def _star_text(entry: dict[str, Any]) -> tuple[int, str]:
    """返回星数与固定 7 位的星条，数据缺失时按星数补齐。"""
    count = _star_count(entry)
    if "stars" in entry:
        return count, "★" * count + "☆" * (7 - count)
    stars = _sanitize_stars(str(entry.get("luckyStar", "")))
    if len(stars) != 7:
        stars = "★" * count + "☆" * (7 - count)
    return count, stars


_RECAST_LIMIT = 1

# 卡池开池当天与次日，全员签文固定为最高档；开池日取自卡池接口。
_POOL_LUCK_TTL = 1800
_POOL_LUCK_CACHE: dict[str, tuple[float, bool]] = {}


def _pool_open_dates(payload: Any) -> set[date]:
    """从卡池接口里取所有开池日期，字段缺失或格式不对的条目跳过。"""
    pools = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(pools, list):
        return set()
    open_dates: set[date] = set()
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        try:
            start = datetime.strptime(str(pool.get("start_time") or ""), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=SHANGHAI_TZ
            )
        except ValueError:
            continue
        open_dates.add(start.date())
    return open_dates


def _is_gacha_lucky_date(day: date, open_dates: set[date]) -> bool:
    """开池当天与其次日算卡池好运日。"""
    return day in open_dates or day - timedelta(days=1) in open_dates


async def _fetch_pool_open_dates() -> set[date]:
    url = str(cfg_api_urls()["gacha_pool_api"]).strip()
    if not url:
        return set()
    client = await get_shared_async_client()
    response = await client.get(url, timeout=8)
    response.raise_for_status()
    return _pool_open_dates(response.json())


async def gacha_lucky_day(day: date | None = None) -> bool:
    """今天是不是卡池好运日；接口不可用时按普通运势处理，稍后重试。"""
    day = day or today()
    key = day.isoformat()
    cached = _POOL_LUCK_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _POOL_LUCK_TTL:
        return cached[1]
    try:
        lucky = _is_gacha_lucky_date(day, await _fetch_pool_open_dates())
    except (httpx.HTTPError, OSError, ValueError):
        logger.opt(exception=True).debug("[Fortune] 卡池开池日获取失败，按普通运势处理")
        lucky = False
    _POOL_LUCK_CACHE.clear()
    _POOL_LUCK_CACHE[key] = (time.monotonic(), lucky)
    return lucky


def _top_fortunes() -> list[dict[str, Any]]:
    """最高档签文（超好运），数据异常时退回全库。"""
    return [item for item in _JRYS_DATA if _star_count(item) == 7] or list(_JRYS_DATA)


_TRIGRAM_BITS = {
    "乾": "111",
    "兑": "110",
    "离": "101",
    "震": "100",
    "巽": "011",
    "坎": "010",
    "艮": "001",
    "坤": "000",
}


def _hexagram_svg(entry: dict[str, Any]) -> str:
    """用六条横线画卦象，避免字体缺字导致 ䷀ 变成豆腐块。"""
    upper = _TRIGRAM_BITS.get(str(entry.get("upper") or ""))
    lower = _TRIGRAM_BITS.get(str(entry.get("lower") or ""))
    if not upper or not lower:
        return ""
    rows: list[str] = []
    for index, bit in enumerate(reversed(lower + upper)):
        y = index * 5
        if bit == "1":
            rows.append(f'<rect x="0" y="{y}" width="22" height="3" rx="1"/>')
        else:
            rows.append(f'<rect x="0" y="{y}" width="9" height="3" rx="1"/>')
            rows.append(f'<rect x="13" y="{y}" width="9" height="3" rx="1"/>')
    return (
        '<svg viewBox="0 0 22 28" xmlns="http://www.w3.org/2000/svg" fill="currentColor">'
        f'{"".join(rows)}</svg>'
    )


def _fortune_source(entry: dict[str, Any]) -> str:
    """签文出处：卦名 + 爻位，有原文时一并附上。"""
    head = str(entry.get("hexagram") or "").strip()
    line = str(entry.get("line") or "")
    original = str(entry.get("original") or "").rstrip("。；;")
    if head and line:
        detail = f"{head} · {line}"
        return f"— {detail}（{original}）" if original else f"— {detail}"
    if head:
        return f"— {head}"
    return "— 今日签文"


def _fortune_seed(user_id: str, record: dict[str, Any]) -> str:
    """种子只含用户与日期；改过命时追加次数，让整张卡整体换一套。"""
    return f"{user_id}:{record.get('time', '')}:{int(record.get('recast', 0))}"


def _pick_fortune(entries: list[dict[str, Any]], rng: random.Random | None = None) -> dict[str, Any]:
    """数据声明 weight 时按权重抽；否则先按星级选档再在档内随机。"""
    picker = random.Random() if rng is None else rng
    if any("weight" in item for item in entries):
        weights = [max(0.0, float(item.get("weight", 1))) for item in entries]
        if sum(weights) > 0:
            return picker.choices(entries, weights=weights, k=1)[0]
    buckets: dict[int, list[dict[str, Any]]] = {}
    for item in entries:
        buckets.setdefault(_star_count(item), []).append(item)
    tiers = sorted(buckets)
    weights = [_STAR_WEIGHTS.get(tier, 1) for tier in tiers]
    tier = picker.choices(tiers, weights=weights, k=1)[0]
    return picker.choice(buckets[tier])


def _dimensions(seed: str, stars: int) -> dict[str, float]:
    rng = random.Random(seed + ":dims")
    base = 0.32 + min(max(stars, 0), 7) / 7 * 0.48
    return {
        axis: round(min(1.0, max(0.2, base + rng.uniform(-0.16, 0.16))), 2)
        for axis in _AXES
    }


def _lucky(seed: str) -> dict[str, Any]:
    rng = random.Random(seed + ":lucky")
    name, color_hex = rng.choice(_COLORS)
    return {
        "color": name,
        "hex": color_hex,
        "number": rng.randint(1, 12),
        "direction": rng.choice(_DIRECTIONS),
        "window": rng.choice(_WINDOWS),
    }


def _activities(seed: str) -> tuple[list[str], list[str]]:
    """宜忌各取两到三项且组别互斥，杜绝“宜早睡、忌熬夜”式矛盾。"""
    rng = random.Random(seed + ":yiji")
    good_items = rng.sample(_GOOD, rng.choice((2, 3)))
    good_groups = {item[1] for item in good_items}
    bad_pool = [item for item in _BAD if item[1] not in good_groups] or list(_BAD)
    bad_items = rng.sample(bad_pool, min(rng.choice((2, 3)), len(bad_pool)))
    return [name for name, _ in good_items], [name for name, _ in bad_items]


def _activity_items(names: list[str]) -> str:
    """宜忌词条渲染为可换行的文字条目，避免三项挤成一行溢出。"""
    return "".join(f'<span class="item">{html.escape(name)}</span>' for name in names)


def _radar_svg(dims: dict[str, float], color: str, size: int = 320) -> str:
    items = list(dims.items())
    cx = cy = size / 2
    radius = size / 2 - 52
    grid, axes_lines, labels, points = [], [], [], []
    for ring in (0.33, 0.66, 1.0):
        vertices = []
        for index in range(len(items)):
            angle = -math.pi / 2 + index * 2 * math.pi / len(items)
            vertices.append(
                f"{cx + radius * ring * math.cos(angle):.1f},{cy + radius * ring * math.sin(angle):.1f}"
            )
        grid.append(
            f'<polygon points="{" ".join(vertices)}" fill="none" '
            'stroke="rgba(255,255,255,.35)" stroke-width="1.2"/>'
        )
    for index, (axis, value) in enumerate(items):
        angle = -math.pi / 2 + index * 2 * math.pi / len(items)
        axes_lines.append(
            f'<line x1="{cx}" y1="{cy}" x2="{cx + radius * math.cos(angle):.1f}" '
            f'y2="{cy + radius * math.sin(angle):.1f}" stroke="rgba(255,255,255,.3)" stroke-width="1.2"/>'
        )
        points.append(
            f"{cx + radius * value * math.cos(angle):.1f},{cy + radius * value * math.sin(angle):.1f}"
        )
        labels.append(
            f'<text x="{cx + (radius + 24) * math.cos(angle):.1f}" '
            f'y="{cy + (radius + 22) * math.sin(angle):.1f}" text-anchor="middle" '
            f'dominant-baseline="middle" font-size="16" font-weight="800" fill="#fff">'
            f"{html.escape(axis)}"
            f'<tspan dx="5" font-size="17" fill="#ffffff" opacity="0.8">{round(value * 100)}</tspan>'
            "</text>"
        )
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        'xmlns="http://www.w3.org/2000/svg">'
        f'{"".join(grid)}{"".join(axes_lines)}'
        f'<path d="M {" L ".join(points)} Z" fill="rgba(216,255,0,.22)" stroke="#d8ff00" stroke-width="2.5"/>'
        f'{"".join(labels)}'
        "</svg>"
    )


def _avatar_uri(name: str, color: str) -> str:
    """用昵称首字生成占位头像，避免为渲染额外请求头像接口。"""
    size = 120
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([0, 0, size - 1, size - 1], fill=color)
    try:
        draw.text((size / 2, size / 2), (name or "旅")[:1], font=load_font(_SHARED_FONT, 54),
                  fill=(255, 255, 255, 255), anchor="mm")
    except Exception:
        logger.opt(exception=True).debug("[Fortune] 头像首字绘制失败")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _image_data_uri(data: bytes) -> str | None:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            mime = "image/png" if (probe.format or "").lower() == "png" else "image/jpeg"
    except (UnidentifiedImageError, OSError):
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def _fetch_avatar_bytes(bot: Any, user_id: str) -> bytes | None:
    """按平台能力取用户真实头像；失败时返回 None，由昵称首字头像兜底。"""
    if bot is None or Capability.AVATAR not in bot.capabilities:
        return None
    now = time.monotonic()
    cached = _AVATAR_CACHE.get(user_id)
    if cached is not None and now - cached[0] < _AVATAR_CACHE_SECONDS:
        return cached[1]
    try:
        url = bot.capabilities.require(Capability.AVATAR).avatar_url(user_id)
        response = await (await get_shared_async_client()).get(str(url), timeout=6)
        response.raise_for_status()
        data = response.content
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except (httpx.HTTPError, OSError, UnsupportedCapability, UnidentifiedImageError):
        logger.opt(exception=True).debug("[Fortune] 头像获取失败")
        return None
    _AVATAR_CACHE[user_id] = (now, data)
    return data


def _font_face_rule() -> str:
    """把随包字体以 data URI 内联，Linux 上不依赖系统字体。"""
    global _FONT_FACE_RULE
    if _FONT_FACE_RULE is None:
        rules: list[str] = []
        for family, path in _FONT_FILES:
            try:
                payload = base64.b64encode(path.read_bytes()).decode()
            except OSError:
                logger.opt(exception=True).debug("[Fortune] 字体读取失败：{}", path.name)
                continue
            rules.append(
                f"@font-face{{font-family:'{family}';"
                f"src:url(data:font/woff2;base64,{payload}) format('woff2');"
                "font-display:swap;}"
            )
        _FONT_FACE_RULE = "".join(rules)
    return _FONT_FACE_RULE


def _template() -> str:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        _TEMPLATE_CACHE = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return _TEMPLATE_CACHE


def _gradient_uri() -> str:
    """无背景图时使用的深色渐变，保持卡片不空白。"""
    global _GRADIENT_URI
    if _GRADIENT_URI is None:
        width, height = 1080, 1920
        image = Image.new("RGB", (width, height), "#241d3a")
        draw = ImageDraw.Draw(image)
        for y in range(height):
            t = y / (height - 1)
            draw.line([(0, y), (width, y)], fill=(int(36 + 50 * t), int(30 + 40 * t), int(58 + 80 * t)))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
        _GRADIENT_URI = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    return _GRADIENT_URI


def build_card_html(
    user_id: str,
    record: dict[str, Any],
    nickname: str,
    background: bytes | None = None,
    avatar: bytes | None = None,
) -> str:
    """按运势卡模板的原始占位符填值；背景与头像内联，避免文件访问。"""
    entry = record.get("fortune") or {}
    stars, star_text = _star_text(entry)
    level = str(entry.get("level") or entry.get("fortuneSummary") or "今日运势")
    seed = _fortune_seed(user_id, record)
    lucky = _lucky(seed)
    good, bad = _activities(seed)
    theme = lucky["hex"]
    good_items = _activity_items(good)
    bad_items = _activity_items(bad)

    avatar_url = _image_data_uri(avatar) if avatar else None
    background_image = _gradient_uri()
    if background:
        try:
            with Image.open(io.BytesIO(background)) as probe:
                mime = "image/png" if (probe.format or "").lower() == "png" else "image/jpeg"
            background_image = f"data:{mime};base64,{base64.b64encode(background).decode()}"
        except (UnidentifiedImageError, OSError):
            logger.opt(exception=True).debug("[Fortune] 背景图无法识别，使用渐变兜底")

    values = {
        # 贴纸按年份走（FORTUNE '26），不必每年手改模板。
        "sticker_text": f"FORTUNE {today_str('%y')}'",
        "font_face_rule": _font_face_rule(),
        "theme_color": theme,
        "background_image": background_image,
        "avatar_url": avatar_url or _avatar_uri(nickname, theme),
        "qq_number": html.escape(nickname or user_id),
        "level": html.escape(level),
        "stars": star_text,
        "fortune_text": html.escape(
            str(
                entry.get("plain")
                or entry.get("signText")
                or entry.get("unsignText")
                or "今天也要好好生活。"
            )
        ),
        "fortune_source": html.escape(_fortune_source(entry)),
        "hexagram_svg": _hexagram_svg(entry),
        "radar_svg": _radar_svg(_dimensions(seed, stars), theme),
        "lucky_color": html.escape(lucky["color"]),
        "lucky_number": str(lucky["number"]),
        "lucky_direction": html.escape(lucky["direction"]),
        "good_activities": good_items,
        "bad_activities": bad_items,
        "current_date": today_str("%Y.%m.%d"),
    }
    return _template().format(**values)


def _default_background_bytes() -> bytes | None:
    """内置默认背景；读不到时返回 None，由渐变继续兜底。"""
    global _DEFAULT_BG
    if _DEFAULT_BG is None:
        try:
            _DEFAULT_BG = _DEFAULT_BG_PATH.read_bytes()
        except OSError:
            logger.opt(exception=True).debug("[Fortune] 默认背景读取失败")
            _DEFAULT_BG = b""
    return _DEFAULT_BG or None


async def _fetch_background_bytes() -> bytes | None:
    """每次现取背景图；接口为空、超时或失败时走渐变兜底。"""
    url = str(cfg_api_urls()["background_api"]).strip()
    if not url:
        return None
    try:
        client = await get_shared_async_client()
        response = await client.get(url, timeout=8)
        response.raise_for_status()
        return response.content or None
    except (httpx.HTTPError, OSError):
        logger.opt(exception=True).debug("[Fortune] 背景图获取失败")
        return None


async def render_card_bytes(
    user_id: str,
    record: dict[str, Any],
    nickname: str,
    *,
    bot: Any = None,
) -> bytes:
    """优先 Playwright 渲染 HTML 卡；任何失败都回退到旧的 PIL 卡片。"""
    background = await _fetch_background_bytes() or _default_background_bytes()
    avatar = await _fetch_avatar_bytes(bot, user_id) if bot is not None else None
    try:
        from ...runtime.browser import render_html

        png = await render_html(
            build_card_html(user_id, record, nickname, background, avatar),
            viewport={"width": 1080, "height": 1920},
            full_page=False,
        )
        try:
            with Image.open(io.BytesIO(png)) as image:
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=90, optimize=True)
                return buffer.getvalue()
        except (UnidentifiedImageError, OSError):
            return png
    except Exception:
        logger.opt(exception=True).debug("[Fortune] HTML 渲染失败，回退 PIL")
    image = None
    if background:
        try:
            image = Image.open(io.BytesIO(background)).convert("RGBA")
        except (UnidentifiedImageError, OSError):
            image = None
    return image_bytes(_generate_fortune_canvas(nickname, record, image))


def _get_or_create_today_fortune(user_id: str, *, gacha_lucky: bool = False) -> tuple[dict[str, Any], bool]:
    """获取或生成当天运势；卡池日整库只从最高档里抽。"""
    if not _JRYS_DATA:
        _load_fortune_defs()
    today = today_str()
    _load_user_fortunes()
    record = _USER_FORTUNES.get(user_id)
    if record and record.get("time") == today:
        # 开池日之前落地的普通签也统一升档，保证当天所有人都是超好运。
        if gacha_lucky and not record.get("gacha_lucky"):
            record["fortune"] = _pick_fortune(_top_fortunes())
            record["gacha_lucky"] = True
            _save_user_fortunes()
        return record, False
    if not _JRYS_DATA:
        raise ValueError("运势库为空，无法生成")
    record = {"fortune": _pick_fortune(_top_fortunes() if gacha_lucky else _JRYS_DATA), "time": today}
    if gacha_lucky:
        record["gacha_lucky"] = True
    _USER_FORTUNES[user_id] = record
    # 生成后立即落盘，不依赖退出钩子。
    _save_user_fortunes()
    return record, True


def _recast_today_fortune(user_id: str, *, gacha_lucky: bool = False) -> tuple[dict[str, Any], bool]:
    """逆天改命：当天重抽一次，返回 (记录, 是否改命成功)。"""
    record, _ = _get_or_create_today_fortune(user_id, gacha_lucky=gacha_lucky)
    if int(record.get("recast", 0)) >= _RECAST_LIMIT:
        return record, False
    current = record.get("fortune") or {}
    base = _top_fortunes() if record.get("gacha_lucky") else _JRYS_DATA
    pool = [item for item in base if item != current] or list(base)
    record["fortune"] = _pick_fortune(pool)
    record["recast"] = int(record.get("recast", 0)) + 1
    _save_user_fortunes()
    return record, True


handle_matcher = on_regex(
    r"^([#＃]|/)(?:今日运势|运势|抽签)", name="today", display_name="今日运势", level="all"
)
recast_matcher = on_regex(
    r"^([#＃]|/)(?:逆天改命|改命)", name="recast", display_name="逆天改命", level="all"
)


@handle_matcher.handle()
async def handle(context: HandlerContext) -> None:
    event = context.event
    nickname = event.user_name or event.user_id
    data, _ = _get_or_create_today_fortune(event.user_id, gacha_lucky=await gacha_lucky_day())
    image = await render_card_bytes(event.user_id, data, nickname, bot=context.bot)
    await context.send(Message([MessageSegment.image(image)]))


@recast_matcher.handle()
async def handle_recast(context: HandlerContext) -> None:
    event = context.event
    record, changed = _recast_today_fortune(event.user_id, gacha_lucky=await gacha_lucky_day())
    if not changed:
        await context.send("今天已经改过一次命了，明天再来。")
        return
    nickname = event.user_name or event.user_id
    image = await render_card_bytes(event.user_id, record, nickname, bot=context.bot)
    await context.send(Message([MessageSegment.image(image)]))
