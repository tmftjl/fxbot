"""表情包制作的原生路由、参数解析与调用统计。"""

from __future__ import annotations

import re
import shlex
import base64
import random
import hashlib
from typing import Optional
from datetime import datetime, timezone, timedelta
from itertools import chain

from loguru import logger
from pypinyin import Style, pinyin
from dateutil.relativedelta import relativedelta

from .plot import plot_duration_counts, plot_meme_and_duration_counts
from .utils import add_timezone, download_url, fetch_image_source
from ...core import Message, Capability, HandlerContext, MessageSegment, UnsupportedCapability
from .config import (
    cfg_notice_prob,
    cfg_whitelist_ids,
    cfg_protected_memes,
    cfg_command_prefixes,
    cfg_random_meme_show_info,
)
from ...utils import tz
from .manager import MemeMode, meme_manager
from .request import (
    MemeInfo,
    MemeKeyWithProperties,
    generate_meme,
    render_meme_list,
    generate_meme_preview,
)
from .recorder import (
    SessionIdType,
    record_meme_generation,
    get_meme_generation_keys,
    get_meme_generation_times,
    get_meme_generation_records,
)
from .exception import MemeGeneratorException
from .protection import protection_manager
from ...core.plugin import scheduled, on_command, on_message
from ...utils.paths import cache_dir
from ...core.matching import command_match

MEME_TRIGGER_KEY = "_memes_trigger"
MEME_MSG_KEY = "_memes_msg"

DEFAULT_PREFIXES = ("#", "＃", "/")

_PRIORITY3_COMMANDS = (
    command_match("随机表情"),
    command_match("更新表情", {"刷新表情"}),
)

_trigger_map: dict[str, list[str]] = {}
_trigger_revision = -1
_trigger_manager: object | None = None


def get_user_id(event) -> str:
    """保留旧开关文件的群/会话键格式。"""
    if event.is_group:
        scene_path = event.target.target_id
    elif event.target.kind in {"guild", "channel"}:
        parent = event.metadata.get("guild_id") or ""
        scene_path = f"{parent}_{event.target.target_id}" if parent else event.target.target_id
    else:
        scene_path = event.user_id
    return f"Unknown_{event.bot_id}_{scene_path}"


def _can_edit(event) -> bool:
    """群管或私聊可修改本会话表情开关。"""
    if event.target.kind == "private":
        return True
    return event.metadata.get("sender_role") in {"admin", "owner"}


def _split_text(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except Exception:
        return text.split()


def _merge_reply_texts(current: list[str], quoted: list[str], max_texts: int) -> list[str]:
    if current or max_texts <= 0:
        return current
    return [text for text in quoted if text.strip()][:max_texts]


def _build_trigger_map() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for meme in meme_manager.get_memes():
        for key in meme.keywords:
            mapping.setdefault(key.lower(), []).append(meme.key)
        for shortcut in meme.shortcuts:
            name = (shortcut.humanized or shortcut.key).strip()
            if name:
                mapping.setdefault(name.lower(), []).append(meme.key)
    return mapping


def _trigger_map_snapshot() -> dict[str, list[str]]:
    """初始化或刷新后重建关键词映射。"""
    global _trigger_map, _trigger_revision, _trigger_manager
    if _trigger_manager is not meme_manager or _trigger_revision != meme_manager.revision:
        _trigger_map = _build_trigger_map()
        _trigger_revision = meme_manager.revision
        _trigger_manager = meme_manager
    return _trigger_map


def _prefixes() -> list[str]:
    configured = cfg_command_prefixes()
    if configured is None:
        return list(DEFAULT_PREFIXES)
    return list(configured)


def _match_prefix(text: str) -> Optional[str]:
    for prefix in _prefixes():
        if prefix == "":
            return ""
        if text.startswith(prefix):
            return prefix
    return None


async def meme_trigger_rule(ctx: HandlerContext) -> bool:
    """识别「前缀 + 关键词」并把参数消息留在本次调用状态。"""
    event = ctx.event
    if any(matcher(event) is not None for matcher in _PRIORITY3_COMMANDS):
        return False
    segments = event.message.segments
    index = 0
    while index < len(segments) and segments[index].type == "reply":
        index += 1
    qq_data = event.metadata.get("qq_data")
    if (
        event.platform == "qq"
        and isinstance(qq_data, dict)
        and qq_data.get("message_type") == 103
        and event.reply is None
    ):
        while index < len(segments) and segments[index].type == "text" and not segments[index].text_value().strip():
            index += 1
        if index < len(segments) and segments[index].type == "mention":
            index += 1
    if index >= len(segments):
        return False

    segment = segments[index]
    if segment.type != "text":
        return False

    segment_text = segment.text_value().lstrip()
    prefix = _match_prefix(segment_text)
    if prefix is None:
        return False
    rest = segment_text[len(prefix) :].lstrip()
    if not rest:
        return False

    parts = rest.split(maxsplit=1)
    trigger = parts[0].strip()
    if not trigger:
        return False
    if not meme_manager.ready:
        meme_manager.request_initialization()
        return False
    if trigger.lower() not in _trigger_map_snapshot():
        return False

    arg_text = parts[1] if len(parts) > 1 else ""
    new_segments: list[MessageSegment] = []
    if arg_text:
        new_segments.append(MessageSegment.text(arg_text.lstrip()))
    new_segments.extend(segments[index + 1 :])
    ctx.state[MEME_TRIGGER_KEY] = trigger
    ctx.state[MEME_MSG_KEY] = Message(new_segments)
    return True


@scheduled(interval=5, name="metadata_init")
async def _initialize_metadata(context) -> None:
    del context
    await meme_manager.initialize_requested()


def _command_argument_message(ctx: HandlerContext) -> Message:
    """从完整消息中还原命令参数部分的消息段。"""
    pattern = re.compile(rf"^(?:#|＃|/){re.escape(ctx.match.command)}(?:\s+(.*))?$")
    segments = ctx.event.message.segments
    for index, segment in enumerate(segments):
        if segment.type != "text":
            continue
        matched = pattern.match(segment.text_value().lstrip())
        if matched is None:
            continue
        result: list[MessageSegment] = []
        if matched.group(1):
            result.append(MessageSegment.text(matched.group(1)))
        result.extend(segments[index + 1 :])
        return Message(result)
    return Message.text(ctx.arguments)


def _first_mention_id(ctx: HandlerContext) -> Optional[str]:
    for segment in _command_argument_message(ctx).segments:
        if segment.type == "mention" and segment.data.get("user_id"):
            return str(segment.data["user_id"])
    return None


def _message_texts(message: Message) -> list[str]:
    text = "".join(segment.text_value() for segment in message.segments).strip()
    return [text] if text else []


def _avatar_url(bot, user_id: str) -> Optional[str]:
    if Capability.AVATAR not in bot.capabilities:
        raise UnsupportedCapability("当前平台未提供头像地址")
    url = bot.capabilities.require(Capability.AVATAR).avatar_url(user_id)
    return str(url) if url else None


async def _avatar_bytes_of(ctx: HandlerContext, user_id: str) -> Optional[bytes]:
    try:
        url = _avatar_url(ctx.bot, user_id)
    except Exception:
        logger.opt(exception=True).debug("当前平台无法获取用户头像")
        return None
    if not url:
        return None
    return await download_url(url)


def _sender_avatar_url(bot, user_id: str) -> Optional[str]:
    try:
        return _avatar_url(bot, user_id)
    except Exception:
        logger.opt(exception=True).debug("当前平台无法获取发送者头像")
        return None


def _decode_source(source: str) -> Optional[bytes]:
    if source.startswith("base64://"):
        try:
            return base64.b64decode(source[len("base64://") :])
        except Exception:
            return None
    if source.startswith("data:"):
        try:
            return base64.b64decode(source.split(",", 1)[1])
        except Exception:
            return None
    return None


async def _download_image_from_segment(bot, segment: MessageSegment) -> Optional[bytes]:
    url = segment.data.get("url")
    if isinstance(url, str) and url:
        decoded = _decode_source(url)
        if decoded is not None:
            return decoded
        if url.startswith(("http://", "https://")):
            return await download_url(url)
    file = segment.data.get("file") or segment.data.get("path")
    if isinstance(file, bytes):
        return file
    if isinstance(file, str) and file:
        decoded = _decode_source(file)
        if decoded is not None:
            return decoded
        if file.startswith(("http://", "https://")):
            return await download_url(file)
    if Capability.MEDIA in bot.capabilities:
        try:
            source = await bot.capabilities.require(Capability.MEDIA).resolve_media(segment)
        except UnsupportedCapability:
            return None
        if isinstance(source, bytes):
            return source
        if isinstance(source, str) and source:
            decoded = _decode_source(source)
            if decoded is not None:
                return decoded
            if source.startswith(("http://", "https://")):
                return await download_url(source)
    return None


async def _collect_reply_inputs(ctx: HandlerContext) -> tuple[list[str], list[bytes], bool]:
    event = ctx.event
    if event.reply is None or Capability.REPLY not in ctx.bot.capabilities:
        return [], [], False
    try:
        reply = await ctx.bot.capabilities.require(Capability.REPLY).get_reply(event)
    except Exception:
        logger.opt(exception=True).warning("读取引用消息失败")
        return [], [], False
    if reply is None:
        return [], [], False

    sources: list[str | bytes] = []
    for segment in reply.message.segments:
        if segment.type != "image":
            continue
        source = segment.data.get("url") or segment.data.get("file") or segment.data.get("file_id")
        if isinstance(source, (str, bytes)) and source:
            sources.append(source)
    images: list[bytes] = []
    for source in sources:
        try:
            data = await fetch_image_source(source)
        except Exception:
            logger.warning("引用图片下载失败", exc_info=True)
            continue
        if data:
            images.append(data)
    return _message_texts(reply.message), images, bool(sources)


def _reprioritize_protected_mentions(
    meme: MemeInfo,
    images: list[bytes],
    image_user_ids: list[Optional[str]],
    image_from_mentions: list[bool],
    has_fallback: bool,
    event,
) -> tuple[list[bytes], list[Optional[str]], list[bool]]:
    """调整受保护用户被 @ 时的头像优先级，单图表情下受保护用户不参与素材竞争。"""
    if (
        meme.params_type.max_images <= 0
        or not protection_manager.is_protected(meme.key)
        or len(images) != len(image_user_ids)
        or len(images) != len(image_from_mentions)
    ):
        return images, image_user_ids, image_from_mentions

    protected_indices = {
        index
        for index, user_id in enumerate(image_user_ids)
        if image_from_mentions[index]
        and user_id
        and protection_manager.is_in_whitelist(user_id, platform=event.platform, bot_id=event.bot_id)
    }
    if not protected_indices:
        return images, image_user_ids, image_from_mentions

    indexed = list(zip(images, image_user_ids, image_from_mentions))
    if meme.params_type.max_images == 1:
        if has_fallback and meme.params_type.min_images == 1 and len(protected_indices) == len(indexed):
            return [], [], []
        ordered = [item for index, item in enumerate(indexed) if index not in protected_indices] + [
            item for index, item in enumerate(indexed) if index in protected_indices
        ]
    else:
        ordered = [item for index, item in enumerate(indexed) if index in protected_indices] + [
            item for index, item in enumerate(indexed) if index not in protected_indices
        ]

    ordered = ordered[: meme.params_type.max_images]
    images, image_user_ids, image_from_mentions = map(list, zip(*ordered))
    return images, image_user_ids, image_from_mentions


async def extract_inputs(
    ctx: HandlerContext,
    meme: Optional[MemeInfo],
    msg: Message,
    *,
    reply_inputs: Optional[tuple[list[str], list[bytes], bool]] = None,
) -> tuple[list[str], list[bytes], list[Optional[str]]]:
    event = ctx.event
    texts: list[str] = []
    images: list[bytes] = []
    image_user_ids: list[Optional[str]] = []
    image_from_mentions: list[bool] = []

    reply_texts, reply_images, has_reply_image = (
        reply_inputs if reply_inputs is not None else await _collect_reply_inputs(ctx)
    )

    has_actual_image = has_reply_image or any(segment.type == "image" for segment in msg.segments)
    has_reply = event.reply is not None
    sender_avatar_url = _sender_avatar_url(ctx.bot, event.user_id)

    for segment in msg.segments:
        if segment.type == "mention":
            target = segment.data.get("user_id")
            if has_reply or has_actual_image:
                continue
            if not target:
                continue
            avatar = await _avatar_bytes_of(ctx, str(target))
            if avatar:
                images.append(avatar)
                image_user_ids.append(str(target))
                image_from_mentions.append(True)

        elif segment.type == "image":
            data = await _download_image_from_segment(ctx.bot, segment)
            if data:
                images.append(data)
                image_user_ids.append(None)
                image_from_mentions.append(False)

        elif segment.type == "text":
            for token in _split_text(segment.text_value()):
                if token.startswith("@") and token[1:]:
                    if has_reply:
                        continue
                    avatar = await _avatar_bytes_of(ctx, token[1:])
                    if avatar:
                        images.append(avatar)
                        image_user_ids.append(token[1:])
                        image_from_mentions.append(True)
                    continue

                if token == "自己":
                    if sender_avatar_url:
                        images.append(await download_url(sender_avatar_url))
                        image_user_ids.append(event.user_id)
                        image_from_mentions.append(False)
                    continue

                if token:
                    texts.append(token)

    if meme is None:
        return texts, images, image_user_ids

    if meme.params_type.min_images > 0 and not images and not reply_images:
        if has_reply_image or (has_reply and not reply_texts):
            await ctx.finish()

    texts = _merge_reply_texts(texts, reply_texts, meme.params_type.max_texts)

    selected_reply_images = reply_images[: max(0, meme.params_type.max_images - len(images))]
    images[:0] = selected_reply_images
    image_user_ids[:0] = [None] * len(selected_reply_images)
    image_from_mentions[:0] = [False] * len(selected_reply_images)

    if meme.params_type.min_images == 2 and len(images) == 1 and sender_avatar_url:
        images.insert(0, await download_url(sender_avatar_url))
        image_user_ids.insert(0, event.user_id)
        image_from_mentions.insert(0, False)

    images, image_user_ids, image_from_mentions = _reprioritize_protected_mentions(
        meme, images, image_user_ids, image_from_mentions, bool(sender_avatar_url), event
    )

    if meme.params_type.min_images == 1 and len(images) == 0 and not has_reply_image and sender_avatar_url:
        images.append(await download_url(sender_avatar_url))
        image_user_ids.append(event.user_id)
        image_from_mentions.append(False)

    return texts, images, image_user_ids


async def find_meme(ctx: HandlerContext, meme_name: str) -> MemeInfo:
    found_memes = meme_manager.find(meme_name)
    found_num = len(found_memes)

    if found_num == 0:
        if searched_memes := meme_manager.search(meme_name, limit=5):
            await ctx.finish(
                f"表情 {meme_name} 不存在，你可能在找：\n"
                + "\n".join(f"* {meme.key} ({'/'.join(meme.keywords)})" for meme in searched_memes)
            )
        await ctx.finish(f"表情 {meme_name} 不存在！")

    if found_num == 1:
        return found_memes[0]

    target_name = meme_name.strip().lower()
    for meme in found_memes:
        if meme.key.lower() == target_name:
            logger.warning(f"[memes] 表情别名重复，已优先选择完全同名项: {meme_name} -> {meme.key}")
            return meme

    chosen = found_memes[0]
    logger.warning(f"[memes] 表情别名重复，已默认选择首个结果: {meme_name} -> {chosen.key}，候选数={found_num}")
    return chosen


def _num_desc(min_num: int, max_num: int) -> str:
    if min_num == max_num:
        return str(min_num)
    return f"{min_num} ~ {max_num}"


def _can_fill_default_texts(meme: MemeInfo, texts: list[str]) -> bool:
    return len(texts) < meme.params_type.min_texts and len(meme.params_type.default_texts) >= meme.params_type.min_texts


async def normalize_meme_params(
    ctx: HandlerContext,
    meme: MemeInfo,
    texts: list[str],
    images: list[bytes],
) -> list[str]:
    """固定处理表情参数数量不匹配。"""
    params = meme.params_type

    if len(images) < params.min_images or len(images) > params.max_images:
        await ctx.finish(f"图片数量不符，图片数量应为 {_num_desc(params.min_images, params.max_images)}")

    if len(texts) > params.max_texts:
        await ctx.finish(f"文字数量不符，文字数量应为 {_num_desc(params.min_texts, params.max_texts)}")

    if len(texts) < params.min_texts:
        if _can_fill_default_texts(meme, texts):
            default_texts = list(params.default_texts)
            texts = texts + default_texts[len(texts) : params.min_texts]
        else:
            await ctx.finish(f"文字数量不符，文字数量应为 {_num_desc(params.min_texts, params.max_texts)}")

    return texts


async def _send_image(ctx: HandlerContext, image: bytes, text: str) -> None:
    segments: list[MessageSegment] = []
    if text:
        segments.append(MessageSegment.text(text))
    segments.append(MessageSegment.image(image))
    await ctx.finish(Message(segments))


def meme_list_cache_dir():
    return cache_dir("memes")


help_cmd = on_command(
    "表情包制作",
    aliases=("表情列表", "头像表情包", "文字表情包"),
    block=True,
    priority=11,
    name="list",
    display_name="表情列表",
    level="member",
)

info_cmd = on_command(
    "表情详情",
    aliases=("表情帮助", "表情示例"),
    block=True,
    priority=11,
    name="detail",
    display_name="表情详情",
    level="member",
)

search_cmd = on_command(
    "表情搜索",
    block=True,
    priority=11,
    name="search",
    display_name="表情搜索",
    level="member",
)

add_whitelist_cmd = on_command(
    "添加保护",
    block=True,
    priority=11,
    name="add_whitelist",
    display_name="添加保护白名单",
    level="superuser",
)
remove_whitelist_cmd = on_command(
    "移除保护",
    block=True,
    priority=11,
    name="remove_whitelist",
    display_name="移除保护白名单",
    level="superuser",
)
protect_meme_cmd = on_command(
    "保护表情",
    block=True,
    priority=11,
    name="protect_meme",
    display_name="保护表情",
    level="superuser",
)
unprotect_meme_cmd = on_command(
    "取消保护表情",
    block=True,
    priority=11,
    name="unprotect_meme",
    display_name="取消保护表情",
    level="superuser",
)
protection_list_cmd = on_command(
    "保护列表",
    block=True,
    priority=11,
    name="protection_list",
    display_name="保护列表",
    level="superuser",
)

statistics_cmd = on_command(
    "表情调用统计",
    aliases=("表情使用统计",),
    block=True,
    priority=11,
    name="statistics",
    display_name="表情调用统计",
    level="member",
)

global_statistics_cmd = on_command(
    "全局表情调用统计",
    block=True,
    priority=11,
    name="global_statistics",
    display_name="全局表情调用统计",
    level="member",
)

block_cmd = on_command(
    "禁用表情",
    block=True,
    priority=11,
    name="block",
    display_name="禁用表情",
    level="member",
)
unblock_cmd = on_command(
    "启用表情",
    block=True,
    priority=11,
    name="unblock",
    display_name="启用表情",
    level="member",
)
block_gl_cmd = on_command(
    "全局禁用表情",
    block=True,
    priority=11,
    name="global_block",
    display_name="全局禁用表情",
    level="superuser",
)
unblock_gl_cmd = on_command(
    "全局启用表情",
    block=True,
    priority=11,
    name="global_unblock",
    display_name="全局启用表情",
    level="superuser",
)
black_list_cmd = on_command(
    "禁用列表",
    aliases=("全局禁用列表",),
    block=True,
    priority=11,
    name="global_block_list",
    display_name="全局禁用列表",
    level="superuser",
)

random_cmd = on_command(
    "随机表情",
    block=True,
    priority=3,
    name="random",
    display_name="随机表情",
    level="member",
)
refresh_cmd = on_command(
    "更新表情",
    aliases=("刷新表情",),
    block=True,
    priority=3,
    name="refresh",
    display_name="更新表情",
    level="superuser",
)

meme_msg_matcher = on_message(
    rule=meme_trigger_rule,
    block=False,
    priority=3,
    name="make",
    display_name="制作表情",
    level="member",
)


@help_cmd.handle()
async def _help(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    memes = sorted(
        meme_manager.get_memes(),
        key=lambda meme: "".join(chain.from_iterable(pinyin(meme.keywords[0], style=Style.TONE3))),
    )
    meme_generation_keys = await get_meme_generation_keys(
        ctx.event,
        SessionIdType.GLOBAL,
        time_start=datetime.now(timezone.utc) - timedelta(days=7),
    )

    meme_list: list[MemeKeyWithProperties] = []
    for meme in memes:
        labels: list[str] = []
        if tz.now() - tz.as_shanghai_local(meme.date_created) < timedelta(days=30):
            labels.append("new")
        if meme_generation_keys.count(meme.key) >= 21:
            labels.append("hot")
        disabled = not meme_manager.check(get_user_id(ctx.event), meme.key)
        meme_list.append(MemeKeyWithProperties(meme_key=meme.key, disabled=disabled, labels=labels))

    meme_list_hashable = [
        (
            {
                "key": meme.key,
                "keywords": meme.keywords,
                "shortcuts": [shortcut.humanized or shortcut.key for shortcut in meme.shortcuts],
                "tags": sorted(meme.tags),
            },
            prop,
        )
        for meme, prop in zip(memes, meme_list)
    ]
    meme_list_hash = hashlib.md5(str(meme_list_hashable).encode("utf8")).hexdigest()
    meme_list_cache_file = meme_list_cache_dir() / f"{meme_list_hash}.jpg"
    if not meme_list_cache_file.exists():
        image = await render_meme_list(
            meme_list,
            text_template="{keywords}",
            add_category_icon=True,
        )
        meme_list_cache_file.write_bytes(image)
    else:
        image = meme_list_cache_file.read_bytes()

    prefixes = cfg_command_prefixes() or []
    hint_prefix = prefixes[0] if prefixes else ""
    text = (
        f"触发方式：关键词{prefixes} 表情名 图片/文字/@某人\n"
        f"例：{hint_prefix}卡提举牌 抽我\n"
        "发送【表情详情+关键词】查看预览\n"
        "群管可 启用/禁用表情+表情名\n"
        "目前支持的表情列表："
    )

    await _send_image(ctx, image, text)


@info_cmd.handle()
async def _info(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    meme_name = ctx.arguments.strip()
    if not meme_name:
        await ctx.finish()

    meme = await find_meme(ctx, meme_name)

    keywords = "、".join([f'"{keyword}"' for keyword in meme.keywords])
    shortcuts = "、".join([f'"{shortcut.humanized or shortcut.key}"' for shortcut in meme.shortcuts])
    tags = "、".join([f'"{tag}"' for tag in meme.tags])

    image_num = f"{meme.params_type.min_images}"
    if meme.params_type.max_images > meme.params_type.min_images:
        image_num += f" ~ {meme.params_type.max_images}"
    text_num = f"{meme.params_type.min_texts}"
    if meme.params_type.max_texts > meme.params_type.min_texts:
        text_num += f" ~ {meme.params_type.max_texts}"
    default_texts = ", ".join([f'"{text}"' for text in meme.params_type.default_texts])

    args_info = ""
    if meme.params_type.args_type:
        for option in meme.params_type.args_type.parser_options:
            args_info += f"\n  * {option.display()}"

    info = (
        f"表情名：{meme.key}"
        + f"\n关键词：{keywords}"
        + (f"\n快捷指令：{shortcuts}" if shortcuts else "")
        + (f"\n标签：{tags}" if tags else "")
        + f"\n需要图片数目：{image_num}"
        + f"\n需要文字数目：{text_num}"
        + (f"\n默认文字：[{default_texts}]" if default_texts else "")
        + (f"\n可选参数：{args_info}" if args_info else "")
        + "\n表情预览："
    )
    image = await generate_meme_preview(meme.key)
    await _send_image(ctx, image, info + "\n")


@search_cmd.handle()
async def _search(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    meme_name = ctx.arguments.strip()
    if not meme_name:
        await ctx.finish()

    memes = meme_manager.search(meme_name, include_tags=True, limit=30)
    if not memes:
        await ctx.finish("未找到相关表情")
    await ctx.finish(
        f"找到 {len(memes)} 个相关表情：\n" + "\n".join(f"* {meme.key} ({'/'.join(meme.keywords)})" for meme in memes)
    )


@add_whitelist_cmd.handle()
async def _add_whitelist(ctx: HandlerContext):
    user_id = _first_mention_id(ctx) or ctx.arguments.strip()
    if not user_id:
        await ctx.finish()
    if protection_manager.add_whitelist(user_id):
        await ctx.finish(f"已将 {user_id} 添加到保护白名单")
    await ctx.finish(f"{user_id} 已在白名单中")


@remove_whitelist_cmd.handle()
async def _remove_whitelist(ctx: HandlerContext):
    user_id = _first_mention_id(ctx) or ctx.arguments.strip()
    if not user_id:
        await ctx.finish()
    if protection_manager.remove_whitelist(user_id):
        await ctx.finish(f"已将 {user_id} 从保护白名单移除")
    await ctx.finish(f"{user_id} 不在白名单中")


@protect_meme_cmd.handle()
async def _protect_meme(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    meme_name = ctx.arguments.strip()
    if not meme_name:
        await ctx.finish()
    meme = await find_meme(ctx, meme_name)
    if protection_manager.add_protected_meme(meme.key):
        await ctx.finish(f"表情 {meme.key} 已添加到保护列表")
    await ctx.finish(f"表情 {meme.key} 已在保护列表中")


@unprotect_meme_cmd.handle()
async def _unprotect_meme(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    meme_name = ctx.arguments.strip()
    if not meme_name:
        await ctx.finish()
    meme = await find_meme(ctx, meme_name)
    if protection_manager.remove_protected_meme(meme.key):
        await ctx.finish(f"表情 {meme.key} 已从保护列表移除")
    await ctx.finish(f"表情 {meme.key} 不在保护列表中")


@protection_list_cmd.handle()
async def _protection_list(ctx: HandlerContext):
    whitelist = cfg_whitelist_ids()
    protected_memes = cfg_protected_memes()
    msg = "【保护配置】\n"
    msg += f"\n保护白名单: {', '.join(whitelist) if whitelist else '（空）'}\n"
    msg += f"\n保护表情: {', '.join(protected_memes) if protected_memes else '（空）'}"
    await ctx.finish(msg)


def _parse_statistics_text(text: str) -> tuple[str, Optional[str]]:
    mapped_type: str = "24h"
    type_map = [
        ({"日", "24小时", "1天"}, "24h"),
        ({"本日", "今日"}, "day"),
        ({"周", "一周", "7天"}, "7d"),
        ({"本周"}, "week"),
        ({"月", "30天"}, "30d"),
        ({"本月", "月度"}, "month"),
        ({"年", "一年"}, "1y"),
        ({"本年", "年度"}, "year"),
    ]
    for keys, typ in type_map:
        if any(key in text for key in keys):
            mapped_type = typ
            break

    meme_name = re.sub(
        r"(日|24小时|1天|本日|今日|周|一周|7天|本周|月|30天|本月|月度|年|一年|本年|年度)",
        " ",
        text,
    ).strip()
    return mapped_type, (meme_name or None)


async def _do_statistics(ctx: HandlerContext, id_type: SessionIdType, raw: str) -> None:
    await meme_manager.ensure_ready()
    typ, meme_name = _parse_statistics_text(raw.strip())

    meme = await find_meme(ctx, meme_name) if meme_name else None

    now = tz.now()
    if typ == "24h":
        start = now - timedelta(days=1)
        td: timedelta | relativedelta = timedelta(hours=1)
        fmt = "%H:%M"
        humanized = "24小时"
    elif typ == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        td = timedelta(hours=1)
        fmt = "%H:%M"
        humanized = "本日"
    elif typ == "7d":
        start = now - timedelta(days=7)
        td = timedelta(days=1)
        fmt = "%m/%d"
        humanized = "7天"
    elif typ == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
        td = timedelta(days=1)
        fmt = "%a"
        humanized = "本周"
    elif typ == "30d":
        start = now - timedelta(days=30)
        td = timedelta(days=1)
        fmt = "%m/%d"
        humanized = "30天"
    elif typ == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        td = timedelta(days=1)
        fmt = "%m/%d"
        humanized = "本月"
    elif typ == "1y":
        start = now - relativedelta(years=1)
        td = relativedelta(months=1)
        fmt = "%y/%m"
        humanized = "一年"
    else:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        td = relativedelta(months=1)
        fmt = "%b"
        humanized = "本年"

    if meme:
        meme_times = await get_meme_generation_times(ctx.event, id_type, meme_key=meme.key, time_start=start)
        meme_keys = [meme.key] * len(meme_times)
    else:
        meme_records = await get_meme_generation_records(ctx.event, id_type, time_start=start)
        meme_records = [record for record in meme_records if meme_manager.get_meme(record.meme_key)]
        meme_times = [record.time for record in meme_records]
        meme_keys = [record.meme_key for record in meme_records]

    if not meme_times:
        await ctx.finish("暂时没有表情调用记录")

    meme_times = [add_timezone(time) for time in meme_times]
    meme_times.sort()

    def fmt_time(time: datetime) -> str:
        if typ in ["24h", "7d", "30d", "1y"]:
            return (time + td).strftime(fmt)
        return time.strftime(fmt)

    duration_counts: dict[str, int] = {}
    stop = start + td
    count = 0
    key = fmt_time(start)
    for time in meme_times:
        while time >= stop:
            duration_counts[key] = count
            key = fmt_time(stop)
            stop += td
            count = 0
        count += 1
    duration_counts[key] = count
    while stop <= now:
        key = fmt_time(stop)
        stop += td
        duration_counts[key] = 0

    key_counts: dict[str, int] = {}
    for meme_key in meme_keys:
        key_counts[meme_key] = key_counts.get(meme_key, 0) + 1
    key_counts = dict(sorted(key_counts.items(), key=lambda item: item[1]))

    if meme:
        title = f"表情“{'/'.join(meme.keywords)}”{humanized}调用统计（总调用次数为 {key_counts.get(meme.key, 0)}）"
        output = await plot_duration_counts(duration_counts, title)
    else:
        title = f"{humanized}表情调用统计（总调用次数为 {sum(key_counts.values())}）"
        meme_counts: dict[str, int] = {}
        for meme_key, count in key_counts.items():
            if found := meme_manager.get_meme(meme_key):
                meme_counts["/".join(found.keywords)] = count
        output = await plot_meme_and_duration_counts(meme_counts, duration_counts, title)

    await _send_image(ctx, output, "")


@statistics_cmd.handle()
async def _statistics(ctx: HandlerContext):
    id_type = SessionIdType.GROUP if ctx.event.is_group else SessionIdType.USER
    await _do_statistics(ctx, id_type, ctx.arguments)


@global_statistics_cmd.handle()
async def _global_statistics(ctx: HandlerContext):
    await _do_statistics(ctx, SessionIdType.GLOBAL, ctx.arguments)


@block_cmd.handle()
async def _block(ctx: HandlerContext):
    if not _can_edit(ctx.event):
        await ctx.finish("权限不足（需要群管或超级用户）")
    meme_name = ctx.arguments.strip()
    if not meme_name:
        await ctx.finish()
    await meme_manager.ensure_ready()

    meme = await find_meme(ctx, meme_name)
    user_key = get_user_id(ctx.event)
    if meme_manager.block(user_key, meme.key):
        await ctx.finish(f"表情 {meme.key} 禁用成功")
    await ctx.finish(f"表情 {meme.key} 已被禁用或已被全局禁用")


@unblock_cmd.handle()
async def _unblock(ctx: HandlerContext):
    if not _can_edit(ctx.event):
        await ctx.finish("权限不足（需要群管或超级用户）")
    meme_name = ctx.arguments.strip()
    if not meme_name:
        await ctx.finish()
    await meme_manager.ensure_ready()

    meme = await find_meme(ctx, meme_name)
    user_key = get_user_id(ctx.event)
    if meme_manager.unblock(user_key, meme.key):
        await ctx.finish(f"表情 {meme.key} 启用成功")
    await ctx.finish(f"表情 {meme.key} 已被全局禁用，请联系超级用户启用")


@block_gl_cmd.handle()
async def _block_gl(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    meme_name = ctx.arguments.strip()
    if not meme_name:
        await ctx.finish()
    meme = await find_meme(ctx, meme_name)
    meme_manager.change_mode(MemeMode.WHITE, meme.key)
    await ctx.finish(f"表情 {meme.key} 已全局禁用")


@unblock_gl_cmd.handle()
async def _unblock_gl(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    meme_name = ctx.arguments.strip()
    if not meme_name:
        await ctx.finish()
    meme = await find_meme(ctx, meme_name)
    meme_manager.change_mode(MemeMode.BLACK, meme.key)
    await ctx.finish(f"表情 {meme.key} 已全局启用")


@black_list_cmd.handle()
async def _black_list(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    black_list = meme_manager.get_black_list()
    if not black_list:
        await ctx.finish("当前没有全局禁用的表情")
    await ctx.finish("当前全局禁用的表情列表：\n" + "\n".join(black_list))


@refresh_cmd.handle()
async def _refresh(ctx: HandlerContext):
    await meme_manager.init()
    await ctx.finish("表情更新成功")


@random_cmd.handle()
async def _random(ctx: HandlerContext):
    await meme_manager.ensure_ready()
    reply_inputs = await _collect_reply_inputs(ctx)
    arg_message = _command_argument_message(ctx)
    base_texts, base_images, _ = await extract_inputs(ctx, None, arg_message, reply_inputs=reply_inputs)
    reply_texts, reply_images, has_reply_image = reply_inputs

    candidates: list[MemeInfo] = []
    user_key = get_user_id(ctx.event)
    for meme in meme_manager.get_memes():
        if not meme_manager.check(user_key, meme.key):
            continue
        images_num = len(base_images)
        images_num += min(len(reply_images), max(0, meme.params_type.max_images - images_num))
        texts_num = len(_merge_reply_texts(base_texts, reply_texts, meme.params_type.max_texts))
        if ctx.event.user_id and images_num == 0 and meme.params_type.min_images == 1 and not has_reply_image:
            if _sender_avatar_url(ctx.bot, ctx.event.user_id):
                images_num = 1
        if meme.params_type.min_images == 2 and images_num == 1 and _sender_avatar_url(ctx.bot, ctx.event.user_id):
            images_num = 2
        if _can_fill_default_texts(meme, base_texts):
            texts_num = meme.params_type.min_texts
        if (
            meme.params_type.min_images <= images_num <= meme.params_type.max_images
            and meme.params_type.min_texts <= texts_num <= meme.params_type.max_texts
        ):
            candidates.append(meme)

    if not candidates:
        await ctx.finish("没有找到符合条件的表情")

    meme = random.choice(candidates)
    texts, images, _ = await extract_inputs(ctx, meme, arg_message, reply_inputs=reply_inputs)
    texts = await normalize_meme_params(ctx, meme, texts, images)

    try:
        result = await generate_meme(meme.key, images, texts, args={})
        await record_meme_generation(ctx.event, meme.key)
    except MemeGeneratorException as error:
        await ctx.finish(error.message)

    text = ""
    if cfg_random_meme_show_info():
        text = f"随机表情：{meme.key} ({'/'.join(meme.keywords)})\n"
    if random.random() < cfg_notice_prob():
        text += "注意避免群聊刷屏哦~群管可启用禁用表情\n"
    await _send_image(ctx, result, text)


@meme_msg_matcher.handle()
async def _meme(ctx: HandlerContext):
    trigger = ctx.state.get(MEME_TRIGGER_KEY, "")
    msg = ctx.state.get(MEME_MSG_KEY)
    if not trigger or not isinstance(msg, Message):
        await ctx.finish()

    meme = await find_meme(ctx, str(trigger))

    user_key = get_user_id(ctx.event)
    if not meme_manager.check(user_key, meme.key):
        await ctx.finish("表情已被禁用")

    texts, images, _ = await extract_inputs(ctx, meme, msg)
    texts = await normalize_meme_params(ctx, meme, texts, images)

    try:
        result = await generate_meme(meme.key, images, texts, args={})
        await record_meme_generation(ctx.event, meme.key)
    except MemeGeneratorException as error:
        await ctx.finish(error.message)

    text = ""
    if random.random() < cfg_notice_prob():
        text += "注意避免群聊刷屏哦~群管可启用禁用表情\n"
    await _send_image(ctx, result, text)
