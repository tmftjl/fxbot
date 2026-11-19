"""QQ 官方适配器：普通群/C2C 使用 ``qqgroup``，频道使用 ``qqguild``。"""

from __future__ import annotations

from typing import Any

from loguru import logger

from fxbot.core import Event, Message as CoreMessage, BotClient, SessionTarget, MessageSegment
from fxbot.platforms.qq.message import keyboard, markdown as qq_markdown

from .base import CoreAdapter, ReceiveScope, outbound_file, outbound_media
from ..models import Message as WireMessage, MessageSend
from .qq_render import render_pages

ADVANCED_SEGMENTS = {"markdown", "template_markdown", "template_buttons", "buttons"}
MEDIA_SEGMENTS = {"image", "record", "video", "file"}


def _remote_url(source: Any) -> str | None:
    """QQ 官方出站远程媒体接受 HTTP(S)；字节由客户端上传。"""
    if isinstance(source, bytes):
        return None
    if isinstance(source, str) and source.startswith(("http://", "https://")):
        return source
    return None


class QQAdapter(CoreAdapter):
    key = "qq"
    name = "QQ 官方"
    PASSIVE_REPLY_LIMIT = 5

    def platform_matches(self, platform: str) -> bool:
        return platform == "qq"

    def matches_send(self, bot_id: str) -> bool:
        return bot_id in {"qqgroup", "qqguild"}

    def receive_bot_id(self, event: Event) -> str:
        if event.target.kind == "channel":
            return "qqguild"
        if event.target.kind == "private" and event.target.target_id.startswith("dms/"):
            return "qqguild"
        return "qqgroup"

    def receive_scope(self, event: Event) -> ReceiveScope:
        if event.target.kind == "channel":
            return ReceiveScope("group", event.target.target_id)
        if event.target.kind == "private":
            if event.target.target_id.startswith("dms/"):
                guild_id = event.metadata.get("guild_id")
                return ReceiveScope("direct", str(guild_id) if guild_id else None)
            return ReceiveScope("direct")
        return ReceiveScope("group", event.target.target_id)

    def target(self, bot: BotClient, msg: MessageSend) -> SessionTarget | None:
        if msg.target_id is None:
            return None
        if msg.bot_id == "qqgroup":
            if msg.target_type == "group":
                kind = "group"
            elif msg.target_type in {None, "direct"}:
                kind = "private"
            else:
                logger.warning("[gscore] QQ 普通群不支持此目标类型: {}", msg.target_type)
                return None
            return SessionTarget(bot.platform, bot.bot_id, kind, str(msg.target_id))
        if msg.bot_id == "qqguild":
            if msg.target_type in {"group", "channel", "sub_channel"}:
                return SessionTarget(bot.platform, bot.bot_id, "channel", str(msg.target_id))
            if msg.target_type == "direct":
                guild_id = self._guild_id(msg.content)
                if guild_id is None:
                    logger.warning("[gscore] QQ 频道私信缺少 guild 上下文，已跳过")
                    return None
                return SessionTarget(bot.platform, bot.bot_id, "private", f"dms/{guild_id}/{msg.target_id}")
            logger.warning("[gscore] QQ 频道不支持此目标类型: {}", msg.target_type)
            return None
        return None

    @staticmethod
    def _guild_id(content: list[WireMessage] | None) -> str | None:
        for segment in content or []:
            if segment.type == "group" and segment.data is not None:
                return str(segment.data)
        return None

    @staticmethod
    def _passive_target(target: SessionTarget) -> bool:
        """普通群与 C2C 使用 msg_id + msg_seq 的被动回复额度。"""
        return target.kind == "group" or (target.kind == "private" and not target.target_id.startswith("dms/"))

    @staticmethod
    def _pair_text_images(frames: list[list[WireMessage]]) -> list[list[WireMessage]]:
        """把文字贴到相邻图片消息里，并合并连续文本。"""
        paired: list[list[WireMessage]] = []
        pending: list[WireMessage] = []
        text_length = 0

        def flush() -> None:
            nonlocal pending, text_length
            if pending:
                paired.append(pending)
                pending = []
                text_length = 0

        for frame in frames:
            if frame and all(segment.type == "text" for segment in frame):
                length = sum(
                    len(segment.data) if isinstance(segment.data, str) else len(str(segment.data or ""))
                    for segment in frame
                )
                if pending and text_length + length > 1900:
                    flush()
                if pending:
                    pending.append(WireMessage("text", "\n"))
                    text_length += 1
                pending.extend(frame)
                text_length += length
            elif len(frame) == 1 and frame[0].type == "image":
                pending.append(frame[0])
                paired.append(pending)
                pending = []
                text_length = 0
            else:
                flush()
                paired.append(frame)
        flush()
        return paired

    @staticmethod
    def _truncate_frames(frames: list[list[WireMessage]]) -> list[list[WireMessage]]:
        """无法渲染时保留前 5 帧，并在最后一条说明剩余数量。"""
        if len(frames) <= QQAdapter.PASSIVE_REPLY_LIMIT:
            return frames
        kept = [list(frame) for frame in frames[: QQAdapter.PASSIVE_REPLY_LIMIT]]
        omitted = len(frames) - len(kept)
        kept[-1].append(WireMessage("text", f"\n[另有 {omitted} 条内容因 QQ 限制未发送]"))
        return kept

    async def prepare_frames(
        self, bot: BotClient, target: SessionTarget, msg: MessageSend, frames: list[list[WireMessage]]
    ) -> list[list[WireMessage]]:
        """被动回复超过上限时配对文字图片，必要时渲染为长图。"""
        if not msg.msg_id or not self._passive_target(target) or len(frames) <= self.PASSIVE_REPLY_LIMIT:
            return frames
        paired = self._pair_text_images(frames)
        if len(paired) <= self.PASSIVE_REPLY_LIMIT:
            return paired
        try:
            pages = await render_pages(paired)
        except Exception:
            logger.opt(exception=True).warning("[gscore] QQ 混合内容长图渲染失败，改为截断发送")
            pages = None
        if pages:
            return [[WireMessage("image", page)] for page in pages[: self.PASSIVE_REPLY_LIMIT]]
        logger.warning("[gscore] QQ 被动回复超过 5 条且无法渲染长图，截断剩余内容")
        return self._truncate_frames(paired)

    def build_message(
        self, bot: BotClient, target: SessionTarget, msg: MessageSend, frame: list[WireMessage]
    ) -> CoreMessage | None:
        restricted = target.kind == "channel" or (target.kind == "private" and target.target_id.startswith("dms/"))
        segments: list[MessageSegment] = []
        if msg.msg_id:
            # 与 gscore 原版一致：被动消息 ID 显式透传给 QQClient，不依赖事件上下文。
            segments.append(MessageSegment("qq:passive", {"msg_id": msg.msg_id}))
        for segment in frame:
            kind, data = segment.type, segment.data
            if kind == "text":
                text = data if isinstance(data, str) else "" if data is None else str(data)
                if text:
                    segments.append(MessageSegment.text(text))
            elif kind == "at":
                # 原版 group_send 不传 at_list，只有频道 guild_send 保留提及。
                if target.kind != "channel":
                    logger.debug("[gscore] QQ 群/C2C/频道私信忽略下行提及段，与原版一致")
                    continue
                user_id = data.get("user_id") if isinstance(data, dict) else data
                if user_id is not None and str(user_id):
                    segments.append(MessageSegment.mention(str(user_id)))
            elif kind == "reply":
                message_id = data.get("message_id") if isinstance(data, dict) else data
                if message_id is not None and str(message_id):
                    segments.append(MessageSegment.reply(str(message_id)))
            elif kind in MEDIA_SEGMENTS:
                if restricted and kind != "image":
                    logger.warning("[gscore] QQ 频道/频道私信仅支持图片媒体，已跳过: {}", kind)
                    continue
                if kind == "file":
                    parsed = outbound_file(data)
                    if parsed is None:
                        logger.debug("[gscore] 无法解析 QQ 文件段")
                        continue
                    name, source = parsed
                    if isinstance(source, str) and _remote_url(source) is None:
                        logger.warning("[gscore] QQ 文件 URL 必须为 HTTP(S)，已跳过: {}", name)
                        continue
                    segments.append(MessageSegment("file", {"file": source, "name": name}))
                    continue
                source = outbound_media(data)
                if source is None:
                    logger.debug("[gscore] 无法解析 QQ 媒体来源: {}", kind)
                    continue
                if isinstance(source, str) and _remote_url(source) is None:
                    logger.warning("[gscore] QQ 远程媒体必须为 HTTP(S)，已跳过: {}", source)
                    continue
                if kind == "image":
                    segments.append(MessageSegment.image(source))
                elif kind == "video":
                    segments.append(MessageSegment.video(source))
                else:
                    segments.append(MessageSegment.voice(source))
            elif kind in ADVANCED_SEGMENTS:
                if restricted:
                    logger.warning("[gscore] QQ 频道/频道私信不支持高级消息段，已跳过: {}", kind)
                    continue
                mapped = self._advanced(kind, data)
                if mapped is not None:
                    segments.append(mapped)
            elif kind == "group":
                continue
            else:
                logger.debug("[gscore] QQ 不支持下行消息段: {}", kind)
        return CoreMessage(segments)

    @staticmethod
    def _advanced(kind: str, data: Any) -> MessageSegment | None:
        if kind == "markdown":
            if isinstance(data, str):
                return qq_markdown(data.replace("link://", ""))
            if isinstance(data, dict):
                return qq_markdown(dict(data))
            logger.warning("[gscore] QQ markdown 数据格式无效，已跳过")
            return None
        if kind == "template_markdown":
            return QQAdapter._template_markdown(data)
        if kind == "template_buttons":
            if isinstance(data, str) and data:
                return keyboard({"id": data})
            if isinstance(data, dict) and data:
                return keyboard(dict(data))
            logger.warning("[gscore] QQ template_buttons 数据格式无效，已跳过")
            return None
        logger.warning("[gscore] QQ raw buttons 无法等价映射到官方键盘，已跳过")
        return None

    @staticmethod
    def _template_markdown(data: Any) -> MessageSegment | None:
        if isinstance(data, str):
            return qq_markdown(data.replace("link://", ""))
        if not isinstance(data, dict):
            logger.warning("[gscore] QQ template_markdown 数据格式无效，已跳过")
            return None
        template_id = data.get("template_id") or data.get("custom_template_id") or data.get("id")
        if template_id is None:
            return qq_markdown(dict(data))
        raw_params = data.get("para") or data.get("params")
        params: list[dict[str, Any]] = []
        if isinstance(raw_params, dict):
            for key, value in raw_params.items():
                values = [str(item) for item in value] if isinstance(value, list) else [str(value)]
                params.append({"key": str(key), "values": values})
        elif isinstance(raw_params, list):
            for item in raw_params:
                if isinstance(item, dict) and "key" in item:
                    value = item.get("values")
                    values = [str(entry) for entry in value] if isinstance(value, list) else [str(value)]
                    params.append({"key": str(item["key"]), "values": values})
        return qq_markdown({"custom_template_id": str(template_id), "params": params})
