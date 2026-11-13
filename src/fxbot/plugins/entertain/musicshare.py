"""本地 music-api 点歌命令。"""

from __future__ import annotations

import io
import re
import json
import time
import uuid
import base64
import asyncio
import binascii
from typing import Any, Literal
from dataclasses import replace, dataclass

from PIL import Image, ImageDraw

from .config import cfg_music
from ...core.types import Event, Message, BotClient, SessionTarget, MessageSegment
from ...utils.http import get_shared_async_client
from ...core.plugin import Service, on_regex, scheduled, on_message
from ...core.router import HandlerContext
from ...utils.cache import cache
from ...utils.fonts import (
    load_font,
    get_shared_font_path,
    draw_text_with_fallback,
    load_fallback_font_pair,
    truncate_text_with_fallback,
)
from ...utils.paths import data_dir
from ...utils.json_file import read_json, write_json

Platform = Literal["qq", "netease"]


_CACHE_TTL = 600
_LOGIN_TTL = 7 * 24 * 60 * 60
_AUTH_POOL_CURSORS: dict[str, int] = {}
_LOGIN_SESSION_FILE = data_dir("entertain") / "music_login_sessions.json"


@dataclass
class Song:
    """音乐搜索结果（字段与 music-api 返回一致）。"""

    id: str
    song: str
    singer: str
    album: str
    duration: int = 0
    index: int = 0
    mid: str | None = None
    media_id: str | None = None
    search_id: str | None = None
    link: str | None = None
    auth: str | None = None
    auth_owner: str | None = None


class MusicLoginRequired(Exception):
    """music-api 提示当前平台需要登录。"""


class MusicPlayUnavailable(Exception):
    """music-api 返回播放失败原因。"""

    def __init__(self, message: str, reason: str = "", detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.detail = detail or {}

    def log_text(self) -> str:
        """生成日志文本。"""
        parts = [self.message]
        if self.reason:
            parts.append(f"reason={self.reason}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return "; ".join(parts)

    @property
    def is_login_related(self) -> bool:
        """是否像登录态失效。"""
        text = f"{self.reason} {self.message}".lower()
        keys = (
            "login",
            "auth",
            "token",
            "session",
            "cookie",
            "uin",
            "登录",
            "登陆",
            "未登录",
            "失效",
        )
        return any(key in text for key in keys)


def _load_login_sessions() -> dict[str, dict[str, Any]]:
    data = read_json(_LOGIN_SESSION_FILE, dict)
    if not isinstance(data, dict):
        raise ValueError("音乐会话必须为对象")
    for key, value in data.items():
        platform, separator, owner = key.partition(":")
        if not separator or platform not in {"qq", "netease"} or SessionTarget.from_key(owner).kind != "private":
            raise ValueError("音乐账号需要完整用户身份")
        if (
            not isinstance(value, dict)
            or value.get("provider") != platform
            or value.get("owner") != owner
            or not isinstance(value.get("accounts"), list)
        ):
            raise ValueError("音乐会话格式错误")
        for account in value["accounts"]:
            if (
                not isinstance(account, dict)
                or not isinstance(account.get("auth"), str)
                or not account["auth"]
                or account.get("owner") != owner
                or account.get("provider") != platform
            ):
                raise ValueError("音乐账号格式错误")
    return data


_LOGIN_SESSIONS: dict[str, dict[str, Any]] = {}


def _normalize_platform(alias: str | None) -> Platform:
    """归一化音乐平台名称。"""
    if not alias:
        default = str(cfg_music()["provider_default"]).lower()
        return "qq" if default == "tencent" else "netease"
    return "qq" if alias.lower() == "qq" else "netease"


def _platform_name_cn(platform: Platform) -> str:
    """返回平台中文名。"""
    return {"qq": "QQ音乐", "netease": "网易云音乐"}[platform]


def _music_api_base() -> str:
    """读取本地 music-api 地址。"""
    return str(cfg_music()["api_base"]).strip().rstrip("/")


def _login_mode() -> str:
    """读取登录账号使用模式。"""
    return cfg_music()["login_mode"]


def _login_hint(platform: Platform) -> str:
    """生成登录提示。"""
    provider_hint = "qq" if platform == "qq" else "网易云"
    if _login_mode() == "per_user":
        return f"请先发送 #音乐登录{provider_hint} 登录自己的 {_platform_name_cn(platform)} 账号。"
    return f"当前没有可共用的 {_platform_name_cn(platform)} 登录账号，请先发送 #音乐登录{provider_hint} 扫码登录。"


def _login_cache_key(owner: str, platform: Platform) -> str:
    """生成登录缓存键。"""
    return f"music_login:{platform}:{owner}"


def _login_session_key(owner: str, platform: Platform) -> str:
    """生成登录持久化键。"""
    return f"{platform}:{owner}"


def _save_login_sessions() -> None:
    persist = {
        key: {"provider": bucket["provider"], "owner": bucket["owner"], "accounts": bucket["accounts"]}
        for key, bucket in _LOGIN_SESSIONS.items()
        if bucket["accounts"]
    }
    write_json(_LOGIN_SESSION_FILE, persist)


def _cache_set(key: str, value: dict[str, Any], ttl: int = _LOGIN_TTL) -> None:
    """写入内存缓存。"""
    cache.set(("musicshare", "login", key), value, ttl=ttl)


def _cache_get(key: str) -> dict[str, Any] | None:
    """读取内存缓存。"""
    return cache.get(("musicshare", "login", key))


def _decode_auth_data(auth: str) -> dict[str, Any]:
    """解码 music-api auth 内的账号数据。"""
    token = str(auth or "").strip()
    if not token:
        return {}
    encoded = token.split(".", 1)[0]
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode((encoded + padding).encode("utf-8")).decode("utf-8")
        data = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


def _auth_account_identity(platform: Platform, account: dict[str, Any]) -> str | None:
    """生成音乐账号身份键，用于同账号重复登录去重。"""
    auth = str(account.get("auth") or "").strip()
    auth_data = _decode_auth_data(auth)

    if platform == "qq":
        value = str(auth_data.get("uin") or account.get("uin") or "").strip()
        if value:
            return f"{platform}:uin:{value.removeprefix('o')}"
        return None

    if platform == "netease":
        profile = auth_data.get("profile") or account.get("profile")
        if isinstance(profile, dict):
            value = str(profile.get("userId") or "").strip()
            if value:
                return f"{platform}:uid:{value}"
        value = str(auth_data.get("userId") or account.get("userId") or "").strip()
        if value:
            return f"{platform}:uid:{value}"
        return None

    return None


def _normalize_login_bucket(owner: str, platform: Platform, data: Any = None) -> dict[str, Any]:
    """规范化登录桶数据。"""
    if not isinstance(data, dict):
        data = {}

    accounts_raw = data.get("accounts")
    accounts = accounts_raw if isinstance(accounts_raw, list) else []
    normalized_accounts: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    for item in accounts:
        if not isinstance(item, dict):
            continue
        auth = str(item.get("auth") or "").strip()
        if not auth:
            continue
        account = {**item, "auth": auth, "owner": owner, "provider": platform}
        identity = _auth_account_identity(platform, account)
        if identity is not None and identity in seen_identities:
            continue
        if identity is not None:
            seen_identities.add(identity)
        normalized_accounts.append(account)

    pending_raw = data.get("pending")
    pending = pending_raw if isinstance(pending_raw, list) else []
    normalized_pending: list[dict[str, Any]] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        login_token = str(item.get("loginToken") or "").strip()
        if login_token:
            normalized_pending.append(
                {
                    **item,
                    "loginToken": login_token,
                    "owner": owner,
                    "provider": platform,
                }
            )

    return {
        "provider": platform,
        "owner": owner,
        "accounts": normalized_accounts,
        "pending": normalized_pending,
    }


async def _get_login_session_data(owner: str, platform: Platform) -> dict[str, Any]:
    """获取登录会话桶。"""
    cache_key = _login_cache_key(owner, platform)
    cached = _cache_get(cache_key)
    if cached is not None:
        bucket = _normalize_login_bucket(owner, platform, cached)
    else:
        bucket = _normalize_login_bucket(owner, platform, _LOGIN_SESSIONS.get(_login_session_key(owner, platform)))
    _cache_set(cache_key, bucket)
    return bucket


async def _save_login_bucket(owner: str, platform: Platform, bucket: dict[str, Any]) -> None:
    """保存登录会话桶。"""
    bucket = _normalize_login_bucket(owner, platform, bucket)
    session_key = _login_session_key(owner, platform)
    if bucket.get("accounts"):
        _LOGIN_SESSIONS[session_key] = bucket
    else:
        _LOGIN_SESSIONS.pop(session_key, None)
    _save_login_sessions()
    _cache_set(_login_cache_key(owner, platform), bucket)


async def _add_pending_login(owner: str, platform: Platform, login_token: str) -> dict[str, Any]:
    """新增待确认登录。"""
    bucket = await _get_login_session_data(owner, platform)
    pending = {
        "id": uuid.uuid4().hex,
        "provider": platform,
        "owner": owner,
        "loginToken": login_token,
        "createdAt": time.time(),
    }
    bucket["pending"].append(pending)
    await _save_login_bucket(owner, platform, bucket)
    return pending


async def _update_pending_login(owner: str, platform: Platform, pending_id: str, login_token: str) -> None:
    """更新待确认登录 token。"""
    bucket = await _get_login_session_data(owner, platform)
    for item in bucket["pending"]:
        if item.get("id") == pending_id:
            item["loginToken"] = login_token
            item["updatedAt"] = time.time()
            break
    await _save_login_bucket(owner, platform, bucket)


async def _remove_pending_login(owner: str, platform: Platform, pending_id: str) -> None:
    """删除待确认登录。"""
    bucket = await _get_login_session_data(owner, platform)
    bucket["pending"] = [item for item in bucket["pending"] if item.get("id") != pending_id]
    await _save_login_bucket(owner, platform, bucket)


async def _add_auth_account(
    owner: str,
    platform: Platform,
    auth: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """新增可用登录账号。"""
    bucket = await _get_login_session_data(owner, platform)
    account: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "provider": platform,
        "owner": owner,
        "auth": auth,
        "createdAt": time.time(),
    }
    if payload:
        for key in ("nickname", "uin", "userId", "accountId", "profile"):
            if payload.get(key) is not None:
                account[key] = payload[key]
    identity = _auth_account_identity(platform, account)
    if identity is not None:
        bucket["accounts"] = [item for item in bucket["accounts"] if _auth_account_identity(platform, item) != identity]
    bucket["accounts"].append(account)
    await _save_login_bucket(owner, platform, bucket)
    return account


async def _remove_auth_account(owner: str, platform: Platform, auth: str) -> None:
    """移除失效登录账号。"""
    bucket = await _get_login_session_data(owner, platform)
    bucket["accounts"] = [item for item in bucket["accounts"] if item.get("auth") != auth]
    await _save_login_bucket(owner, platform, bucket)


def _owners_with_platform(platform: Platform) -> list[str]:
    """列出拥有该平台登录账号的 owner。"""
    owners: list[str] = []
    prefix = f"{platform}:"
    for key in _LOGIN_SESSIONS:
        if key.startswith(prefix):
            owners.append(key.split(":", 1)[1])
    return list(dict.fromkeys(owners))


async def _auth_candidates(user_id: str, platform: Platform) -> list[tuple[str, dict[str, Any]]]:
    """获取可用登录账号候选。"""
    owners = [user_id] if _login_mode() == "per_user" else _owners_with_platform(platform)
    if _login_mode() == "shared" and user_id not in owners:
        owners.append(user_id)

    scope = SessionTarget.from_key(user_id).bot_key()
    owners = [owner for owner in owners if SessionTarget.from_key(owner).bot_key() == scope]
    candidates: list[tuple[str, dict[str, Any]]] = []
    for owner in owners:
        bucket = await _get_login_session_data(owner, platform)
        for account in bucket.get("accounts", []):
            auth = str(account.get("auth") or "").strip()
            if auth:
                candidates.append((owner, account))
    return candidates


async def _next_auth_account(
    user_id: str,
    platform: Platform,
    *,
    exclude: set[str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """轮询选择一个登录账号。"""
    candidates = [
        (owner, account)
        for owner, account in await _auth_candidates(user_id, platform)
        if str(account.get("auth") or "") not in (exclude or set())
    ]
    if not candidates:
        return None
    cursor_key = (
        f"{SessionTarget.from_key(user_id).bot_key()}:{platform}"
        if _login_mode() == "shared"
        else f"{user_id}:{platform}"
    )
    index = _AUTH_POOL_CURSORS.get(cursor_key, 0) % len(candidates)
    _AUTH_POOL_CURSORS[cursor_key] = index + 1
    return candidates[index]


def _decode_data_image(image: str) -> bytes:
    """解码 music-api 返回的二维码图片（data:image/... 格式）。"""
    _, encoded = image.split(",", 1)
    return base64.b64decode(encoded)


async def _music_api_get(path: str, params: dict[str, Any], allow_error_body: bool = False) -> dict[str, Any]:
    """请求本地 music-api。"""
    url = f"{_music_api_base()}{path}"
    client = await get_shared_async_client()
    response = await client.get(url, params=params)
    if response.status_code in {400, 401}:
        try:
            error_text = str((response.json() or {}).get("error") or "")
        except ValueError:
            error_text = response.text
        lowered = error_text.lower()
        login_keys = ("auth", "token", "session", "login", "logged")
        if response.status_code == 401 or any(key in lowered for key in login_keys):
            raise MusicLoginRequired()
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("music-api 返回格式异常")
    if data.get("error") and not allow_error_body:
        raise RuntimeError(str(data["error"]))
    return data


def _quality_for_api(platform: Platform) -> str:
    """转换本地 music-api 音质参数。"""
    config = cfg_music()
    if platform == "qq":
        qualities = ["m4a", "128", "320", "flac"]
        value = config["qq_quality"]
    else:
        qualities = [
            "standard",
            "higher",
            "exhigh",
            "lossless",
            "hires",
            "jyeffect",
            "sky",
            "jymaster",
        ]
        value = config["netease_quality"]
    level = int(value)
    level = max(1, min(level, len(qualities)))
    return qualities[level - 1]


async def _search_songs_api(
    platform: Platform,
    keyword: str,
    auth: str | None = None,
    auth_owner: str | None = None,
) -> list[Song]:
    """通过本地 music-api 搜索歌曲。"""
    if not auth:
        raise MusicLoginRequired()
    limit = int(cfg_music()["search_num"])
    data = await _music_api_get(
        "/api/search",
        {"provider": platform, "key": keyword, "limit": limit, "auth": auth},
    )
    search_id = data.get("searchId")
    items = data.get("songs", [])
    if not isinstance(items, list):
        items = []

    results: list[Song] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        song_id = str(item["id"])
        mid = item.get("songmid")
        media_id = item.get("mediaId")
        duration = int(item.get("duration", 0) or 0)
        name = str(item["name"])
        singer = str(item.get("singer", "未知歌手") or "未知歌手")
        album = str(item.get("album", "") or "")
        index = int(item["index"])

        if platform == "qq" and mid:
            link = f"https://i.y.qq.com/v8/playsong.html?songmid={mid}&type=0"
        elif platform == "netease" and song_id:
            link = f"https://music.163.com/#/song?id={song_id}"
        else:
            link = None

        results.append(
            Song(
                id=song_id,
                mid=str(mid) if mid else None,
                song=name,
                singer=singer,
                album=album,
                duration=duration,
                index=index,
                link=link,
                search_id=str(search_id) if search_id else None,
                auth=auth,
                auth_owner=auth_owner,
                media_id=str(media_id) if media_id else None,
            )
        )
    return results


def _format_play_error(data: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """格式化播放失败响应。"""
    message = str(data.get("error") or "").strip()
    reason = str(data.get("reason") or "").strip()
    detail_raw = data.get("detail")
    detail = detail_raw if isinstance(detail_raw, dict) else {}
    return message, reason, detail


async def _get_song_url_api(
    platform: Platform,
    song: Song,
    auth: str | None = None,
) -> str | None:
    """通过本地 music-api 获取播放链接。"""
    token = auth or song.auth
    if not token:
        raise MusicLoginRequired()
    params: dict[str, Any] = {
        "provider": platform,
        "quality": _quality_for_api(platform),
        "auth": token,
    }
    if song.search_id is not None:
        params["searchId"] = song.search_id
        params["index"] = song.index
    elif platform == "qq":
        params["songmid"] = song.mid or song.id
        if song.media_id:
            params["mediaId"] = song.media_id
    else:
        params["id"] = song.id

    data = await _music_api_get("/api/play", params, allow_error_body=True)
    url = data.get("url")
    if url:
        return str(url)
    if data.get("error") or data.get("reason") or data.get("detail"):
        message, reason, detail = _format_play_error(data)
        raise MusicPlayUnavailable(message or "music-api 未返回播放链接", reason=reason, detail=detail)
    return None


async def _search_songs_with_pool(user_id: str, platform: Platform, keyword: str) -> list[Song]:
    """用登录账号池搜索歌曲。"""
    tried: set[str] = set()
    while True:
        selected = await _next_auth_account(user_id, platform, exclude=tried)
        if not selected:
            raise MusicLoginRequired()
        owner, account = selected
        auth = str(account.get("auth") or "")
        tried.add(auth)
        try:
            return await _search_songs_api(platform, keyword, auth=auth, auth_owner=owner)
        except MusicLoginRequired:
            await _remove_auth_account(owner, platform, auth)


async def _get_song_url_with_pool(
    user_id: str, platform: Platform, song: Song, *, bot: BotClient | None = None
) -> str | None:
    """用登录账号池获取播放链接。"""
    tried: set[str] = set()
    last_play_error: MusicPlayUnavailable | None = None
    if song.auth:
        tried.add(song.auth)
        try:
            url = await _get_song_url_api(platform, song, auth=song.auth)
            if url:
                return url
        except MusicLoginRequired:
            if song.auth_owner:
                await _remove_auth_account(song.auth_owner, platform, song.auth)
        except MusicPlayUnavailable as exc:
            last_play_error = exc
            if exc.is_login_related and song.auth_owner:
                await _remove_auth_account(song.auth_owner, platform, song.auth)

    while True:
        selected = await _next_auth_account(user_id, platform, exclude=tried)
        if not selected:
            if last_play_error:
                if last_play_error.is_login_related:
                    raise MusicLoginRequired()
                raise last_play_error
            if tried:
                return None
            raise MusicLoginRequired()
        owner, account = selected
        auth = str(account.get("auth") or "")
        tried.add(auth)
        fallback_song = replace(song, search_id=None, auth=auth, auth_owner=owner)
        try:
            url = await _get_song_url_api(platform, fallback_song, auth=auth)
            if url:
                return url
        except MusicLoginRequired:
            await _remove_auth_account(owner, platform, auth)
        except MusicPlayUnavailable as exc:
            last_play_error = exc
            if exc.is_login_related:
                await _remove_auth_account(owner, platform, auth)


def _draw_music_list(platform: Platform, keyword: str, songs: list[Song]) -> bytes:
    """按旧版双列卡片排版绘制搜索结果。"""
    bg_color = (240, 242, 245)
    header_color = (64, 84, 180)
    card_bg = (255, 255, 255)
    text_main = (30, 30, 30)
    text_sub = (100, 100, 100)
    accent = (64, 84, 180)

    padding = 30
    columns = 2
    gap_x = 20
    gap_y = 15
    card_h = 70
    col_w = 400
    font_path = get_shared_font_path()

    font_title_fb = load_fallback_font_pair("FZB.ttf", 36)
    font_sub = load_font(font_path, 22)
    font_song_fb = load_fallback_font_pair("FZB.ttf", 26)
    font_artist_fb = load_fallback_font_pair("FZB.ttf", 20)
    font_badge = load_font(font_path, 20)
    font_footer = load_font(font_path, 18)

    count = min(len(songs), 20)
    rows = (count + columns - 1) // columns
    header_h = 120
    list_h = rows * card_h + max(rows - 1, 0) * gap_y
    footer_h = 50
    width = padding * 2 + col_w * columns + gap_x * (columns - 1)
    height = header_h + list_h + footer_h + padding

    image = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(image)
    draw.rectangle([(0, 0), (width, header_h)], fill=header_color)
    draw_text_with_fallback(
        draw,
        (padding, 25),
        f"搜索结果: {keyword}",
        primary=font_title_fb[0],
        fallback=font_title_fb[1],
        fill=(255, 255, 255),
    )
    draw.text(
        (padding, 75),
        f"来源: {_platform_name_cn(platform)} | 共找到 {len(songs)} 首歌曲",
        font=font_sub,
        fill=(220, 220, 255),
    )

    start_y = header_h + 20
    for index, song in enumerate(songs[:20]):
        row = index // columns
        col = index % columns
        x = padding + col * (col_w + gap_x)
        y = start_y + row * (card_h + gap_y)
        draw.rounded_rectangle([(x, y), (x + col_w, y + card_h)], radius=8, fill=card_bg)

        badge_size = 36
        bx = x + 15
        by = y + (card_h - badge_size) // 2
        draw.ellipse([(bx, by), (bx + badge_size, by + badge_size)], fill=bg_color)
        idx_str = str(index + 1)
        bbox = draw.textbbox((0, 0), idx_str, font=font_badge)
        draw.text(
            (
                bx + (badge_size - (bbox[2] - bbox[0])) / 2,
                by + (badge_size - (bbox[3] - bbox[1])) / 2 - 2,
            ),
            idx_str,
            fill=accent,
            font=font_badge,
        )

        text_x = bx + badge_size + 15
        content_w = col_w - (text_x - x) - 10
        song_name = truncate_text_with_fallback(
            song.song,
            content_w,
            primary=font_song_fb[0],
            fallback=font_song_fb[1],
        )
        draw_text_with_fallback(
            draw,
            (text_x, y + 12),
            song_name,
            primary=font_song_fb[0],
            fallback=font_song_fb[1],
            fill=text_main,
        )
        draw_text_with_fallback(
            draw,
            (text_x, y + 42),
            song.singer,
            primary=font_artist_fb[0],
            fallback=font_artist_fb[1],
            fill=text_sub,
        )

    footer = "发送 #序号 (如 #1) 即可播放"
    bbox = draw.textbbox((0, 0), footer, font=font_footer)
    draw.text(
        ((width - (bbox[2] - bbox[0])) / 2, height - 30),
        footer,
        fill=(150, 150, 150),
        font=font_footer,
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Music(Service):
    """原生点歌路由及应用拥有的扫码任务。"""

    LOGIN = r"^[#＃](?:音乐登录|点歌登录)\s*(qq|网易云|netease)?\s*$"
    POLL = r"^[#＃](?:音乐登录状态|点歌登录状态)\s*(qq|网易云|netease)?\s*$"
    SEARCH = r"^[#＃]点歌(?:(qq|网易云|netease))?\s*(.*)"

    def __init__(self, application, authorizer):
        self.application = application
        self.authorizer = authorizer
        self.poll_lock = asyncio.Lock()
        self.selections = {}
        self.pending = {}
        sessions = _load_login_sessions()
        _LOGIN_SESSIONS.update(sessions)

    @staticmethod
    def owner(event: Event) -> str:
        return SessionTarget(event.platform, event.bot_id, "private", event.user_id).key()

    @staticmethod
    def selection_key(event: Event):
        return event.target.key(), event.user_id

    def match_select(self, event: Event) -> str | None:
        if not event.is_message:
            return None
        match = re.fullmatch(r"[#＃](\d+)", event.message.plain_text().strip())
        item = self.selections.get(self.selection_key(event))
        if not match or not item or item[0] <= time.monotonic():
            return None
        if not 1 <= int(match[1]) <= len(item[2]):
            return None
        return str(int(match[1]) - 1)

    async def close(self) -> None:
        for owner, platform, pending_id in tuple(self.pending):
            await _remove_pending_login(owner, platform, pending_id)
        self.pending.clear()
        self.selections.clear()

    async def valid(self, context, permission):
        return self.application.is_registered(context.bot) and await self.authorizer.check(permission, context.event)

    async def login(self, context: HandlerContext) -> None:
        if context.event.target.kind != "private":
            await context.finish("请私聊发送音乐登录命令，避免在群内暴露登录二维码")
        platform = _normalize_platform(re.match(self.LOGIN, context.arguments)[1])
        data = await _music_api_get("/api/login/qr", {"provider": platform})
        token, image = data.get("loginToken"), data.get("image")
        if not isinstance(token, str) or not token or not isinstance(image, str) or not image.startswith("data:image/"):
            await context.finish("音乐接口未返回有效二维码")
        raw = _decode_data_image(image)
        if not await self.valid(context, "entertain:music_login"):
            return
        owner = self.owner(context.event)
        pending = await _add_pending_login(owner, platform, token)
        self.pending[(owner, platform, pending["id"])] = (context, time.monotonic() + 120)
        try:
            await context.send(
                Message(
                    [MessageSegment.text("请扫码确认登录，可发送 #音乐登录状态 检查进度"), MessageSegment.image(raw)]
                )
            )
        except BaseException:
            await _remove_pending_login(owner, platform, pending["id"])
            self.pending.pop((owner, platform, pending["id"]), None)
            raise

    async def poll_one(self, context, owner, platform, pending):
        async with self.poll_lock:
            bucket = await _get_login_session_data(owner, platform)
            if not any(item["id"] == pending["id"] for item in bucket["pending"]):
                return True
            return await self._poll_one(context, owner, platform, pending)

    async def _poll_one(self, context, owner, platform, pending):
        data = await _music_api_get("/api/login/poll", {"provider": platform, "loginToken": pending["loginToken"]})
        if not await self.valid(context, "entertain:music_login"):
            return True
        if data.get("loggedIn"):
            auth = data.get("auth")
            if not isinstance(auth, str) or not auth:
                raise ValueError("音乐接口登录成功但未返回 auth")
            await _add_auth_account(owner, platform, auth, data)
            await _remove_pending_login(owner, platform, pending["id"])
            await context.send(f"{_platform_name_cn(platform)} 登录成功")
            return True
        if data.get("status") == "expired":
            await _remove_pending_login(owner, platform, pending["id"])
            await context.send("音乐登录二维码已过期，请重新登录")
            return True
        if token := data.get("loginToken"):
            if not isinstance(token, str):
                raise ValueError("音乐接口 token 格式错误")
            await _update_pending_login(owner, platform, pending["id"], token)
        return False

    async def check_pending(self):
        for key, (context, deadline) in tuple(self.pending.items()):
            owner, platform, pending_id = key
            if time.monotonic() >= deadline or not await self.valid(context, "entertain:music_login"):
                await _remove_pending_login(owner, platform, pending_id)
                self.pending.pop(key, None)
                continue
            bucket = await _get_login_session_data(owner, platform)
            pending = next((item for item in bucket["pending"] if item["id"] == pending_id), None)
            if pending is None or await self.poll_one(context, owner, platform, pending):
                await _remove_pending_login(owner, platform, pending_id)
                self.pending.pop(key, None)

    async def poll(self, context: HandlerContext) -> None:
        if context.event.target.kind != "private":
            await context.finish("请在私聊检查音乐登录状态")
        platform = _normalize_platform(re.match(self.POLL, context.arguments)[1])
        owner = self.owner(context.event)
        bucket = await _get_login_session_data(owner, platform)
        for pending in tuple(bucket["pending"]):
            if await self.poll_one(context, owner, platform, pending):
                return
        await context.send(f"已登录 {len(bucket['accounts'])} 个账号" if bucket["accounts"] else "尚未登录，请扫码确认")

    async def search(self, context: HandlerContext) -> None:
        matched = re.match(self.SEARCH, context.arguments)
        platform = _normalize_platform(matched[1])
        keyword = matched[2].strip()
        if not keyword:
            await context.finish("请提供关键词，例如：#点歌 晴天")
        try:
            songs = await _search_songs_with_pool(self.owner(context.event), platform, keyword)
        except MusicLoginRequired:
            await context.finish(_login_hint(platform))
        if not await self.valid(context, "entertain:search"):
            return
        if not songs:
            await context.finish("未找到相关歌曲")
        self.selections[self.selection_key(context.event)] = (time.monotonic() + _CACHE_TTL, platform, songs)
        await context.send(Message([MessageSegment.image(_draw_music_list(platform, keyword, songs))]))

    async def select(self, context: HandlerContext) -> None:
        item = self.selections.get(self.selection_key(context.event))
        if item is None or item[0] <= time.monotonic():
            return
        _, platform, songs = item
        index = int(context.arguments)
        if not 0 <= index < len(songs):
            await context.finish("序号超出搜索结果范围")
        try:
            url = await _get_song_url_with_pool(self.owner(context.event), platform, songs[index], bot=context.bot)
        except MusicLoginRequired:
            await context.finish(_login_hint(platform))
        except MusicPlayUnavailable:
            await context.finish("该歌曲暂时无法播放")
        if url and await self.valid(context, "entertain:select"):
            await context.send(Message([MessageSegment.voice(url)]))


@on_regex(Music.LOGIN, name="music_login", display_name="音乐登录", level="all", priority=4)
async def login(context):
    await context.service(music).login(context)


@on_regex(Music.POLL, name="music_login_poll", display_name="音乐登录状态", level="all", priority=4)
async def poll(context):
    await context.service(music).poll(context)


@on_regex(Music.SEARCH, name="search", display_name="点歌", level="all")
async def search(context):
    await context.service(music).search(context)


async def has_selection(context):
    return context.service(music).match_select(context.event) is not None


@on_message(name="select", display_name="选择歌曲", level="all", when=has_selection)
async def select(context):
    from dataclasses import replace

    index = context.service(music).match_select(context.event)
    if index is not None:
        await context.service(music).select(replace(context, arguments=index))


def music(runtime):
    return Music(runtime.application, runtime.authorizer)


@scheduled(interval=2, name="music_login")
async def check_logins(context):
    await context.service(music).check_pending()
