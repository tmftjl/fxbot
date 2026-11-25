"""适配器协议与平台无关实现；新增平台只需实现一个 Adapter 子类。"""

from __future__ import annotations

import uuid
import base64
import binascii
from abc import ABC, abstractmethod
from typing import Any
from pathlib import Path
from dataclasses import dataclass

from loguru import logger

from fxbot.core import (
    Event,
    Message as CoreMessage,
    BotClient,
    Capability,
    SessionTarget,
    MessageSegment,
    ApplicationServices,
    UnsupportedCapability,
)
from fxbot.core.errors import ActionFailed
from fxbot.utils.paths import cache_dir
from fxbot.permission.identity import is_superuser_event

from ..models import Message as WireMessage, MessageSend, MessageReceive

COMMAND_PREFIXES = ("#", "＃", "/")
CONTROL_TYPES = {"excute_delete_message", "excute_ban_user"}
# gsuid 给群目标下发时会在 content 末尾附带 ``group`` 会话标记，它不是可发送内容。
MARKER_TYPES = {"group"}
ROLES = {"owner", "admin", "member"}
META_NAMES = {"member_joined": "user_join_group", "member_left": "user_exit_group"}


@dataclass(frozen=True, slots=True)
class ReceiveScope:
    """事件在上行协议中的会话类型。"""

    user_type: str
    group_id: str | None = None


@dataclass(frozen=True, slots=True)
class MetaEvent:
    """与具体平台无关的 meta 事件；最终编码为 content 中的 meta-* 段。"""

    name: str
    data: dict[str, Any]
    bot_id: str | None = None
    user_type: str | None = None


@dataclass(frozen=True, slots=True)
class SendResult:
    """一帧下行发送得到的平台消息 ID；多气泡时为 list。"""

    recall_id: str | list[str] | None = None


def _decode_base64(value: str) -> bytes | None:
    """只接受严格 base64，避免把普通文件名误当成媒体内容。"""
    text = value.strip()
    if text.startswith("base64://"):
        text = text[len("base64://") :]
    elif text.startswith("data:"):
        _header, separator, payload = text.partition(",")
        if not separator or ";base64" not in _header:
            return None
        text = payload
    if not text:
        return None
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return None


def media_value(data: Any) -> str | bytes | None:
    """从消息段数据中取出第一个明确的媒体来源。"""
    if isinstance(data, (str, bytes)):
        return data or None
    if not isinstance(data, dict):
        return None
    for key in ("url", "path", "file", "data"):
        value = data.get(key)
        if isinstance(value, bytes):
            return value or None
        if isinstance(value, str) and value:
            return value
    return None


def wire_media_reference(value: Any) -> str | None:
    """把平台媒体来源归一为 gsuid 的裸 URL 或 base64://。"""
    if isinstance(value, bytes):
        return "base64://" + base64.b64encode(value).decode("ascii")
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("link://"):
        value = value[len("link://") :]
    if value.startswith("base64://"):
        return value
    if value.startswith("data:"):
        decoded = _decode_base64(value)
        return None if decoded is None else "base64://" + base64.b64encode(decoded).decode("ascii")
    if value.startswith(("http://", "https://")):
        # gsuid 下游插件会直接使用图片数据，HTTP 地址保持裸 URL。
        return value
    decoded = _decode_base64(value)
    if decoded is not None:
        return "base64://" + base64.b64encode(decoded).decode("ascii")
    return None


def wire_file_reference(value: Any) -> str | None:
    """文件段使用 gsuid 约定的裸 URL 或裸 base64，不带协议前缀。"""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("link://"):
        value = value[len("link://") :]
    if value.startswith("base64://"):
        value = value[len("base64://") :]
    if value.startswith(("http://", "https://")):
        return value
    decoded = _decode_base64(value)
    return None if decoded is None else base64.b64encode(decoded).decode("ascii")


def outbound_media(data: Any) -> bytes | str | None:
    """下行媒体：base64 转为字节，HTTP(S) 保留为远端地址。"""
    value = media_value(data)
    if isinstance(value, bytes):
        return value or None
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("link://"):
        value = value[len("link://") :]
        return value if value.startswith(("http://", "https://")) else None
    if value.startswith(("http://", "https://")):
        return value
    return _decode_base64(value)


def outbound_file(data: Any) -> tuple[str, bytes | str] | None:
    """解析 gsuid 的 ``文件名|内容`` 文件段。"""
    if not isinstance(data, str) or not data:
        return None
    name, separator, payload = data.partition("|")
    if not separator:
        name, payload = "file.bin", data
    name = name.strip() or "file.bin"
    source = outbound_media(payload)
    if source is None:
        return None
    return name, source


def extract_message_ids(result: Any) -> list[str]:
    """从平台发送回执中提取消息 ID，不把布尔值或空值当成 ID。"""
    if isinstance(result, dict):
        for key in ("message_id", "id"):
            value = result.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (str, int)) and str(value):
                return [str(value)]
        data = result.get("data")
        return extract_message_ids(data) if isinstance(data, dict) else []
    if isinstance(result, list):
        ids: list[str] = []
        for item in result:
            ids.extend(extract_message_ids(item))
        return ids
    if isinstance(result, bool) or result is None:
        return []
    if isinstance(result, (str, int)) and str(result):
        return [str(result)]
    return []


def combine_ids(ids: list[str]) -> str | list[str] | None:
    """无 id 返回 None，单气泡返回 str，多气泡返回 list。"""
    unique: list[str] = []
    for value in ids:
        if value not in unique:
            unique.append(value)
    if not unique:
        return None
    return unique[0] if len(unique) == 1 else unique


def user_pm(event: Event, services: ApplicationServices) -> int:
    """superuser 使用 FxBot 配置身份（完整键或裸用户 ID），其余按平台成员角色映射。"""
    system = services.configuration.get_system().get("system")
    if isinstance(system, dict) and is_superuser_event(system, event):
        return 1
    role = event.metadata.get("sender_role")
    if role == "owner":
        return 2
    if role == "admin":
        return 3
    return 6


def _node_item(item: Any) -> WireMessage | None:
    """合并转发节点内只接受统一消息段或标准 dict。"""
    if isinstance(item, WireMessage):
        return item
    if isinstance(item, dict) and isinstance(item.get("type"), str):
        return WireMessage(item["type"], item.get("data"))
    return None


class Adapter(ABC):
    """平台适配器只负责事件映射、目标解析和消息编码。"""

    key: str
    name: str

    @abstractmethod
    def matches(self, event: Event) -> bool:
        """判断入站事件是否属于本适配器。"""

    @abstractmethod
    def matches_send(self, bot_id: str) -> bool:
        """判断下行帧的 bot_id 是否由本适配器处理。"""

    @abstractmethod
    def receive_bot_id(self, event: Event) -> str:
        """返回上行协议使用的 bot_id。"""

    @abstractmethod
    def receive_scope(self, event: Event) -> ReceiveScope:
        """返回上行协议使用的会话类型和群/频道 ID。"""

    @abstractmethod
    async def build_receive(
        self,
        event: Event,
        *,
        bot: BotClient,
        services: ApplicationServices,
        reply_image: bool,
    ) -> MessageReceive | None:
        """把平台事件转换为 gsuid 上行事件。"""

    @abstractmethod
    def build_meta(self, event: Event) -> MetaEvent | None:
        """把通知事件转换为 meta-*；无法映射时返回 None。"""

    @abstractmethod
    async def send(self, services: ApplicationServices, msg: MessageSend) -> SendResult:
        """选择在线 Bot、编码消息并返回平台回执 ID。"""


class CoreAdapter(Adapter):
    """基于 FxBot 核心事件与消息段的通用实现，平台差异通过钩子覆盖。"""

    def matches(self, event: Event) -> bool:
        return self.platform_matches(event.platform)

    @abstractmethod
    def platform_matches(self, platform: str) -> bool:
        """匹配 FxBot 平台标识。"""

    def receive_scope(self, event: Event) -> ReceiveScope:
        if event.target.kind == "group":
            return ReceiveScope("group", event.target.target_id)
        return ReceiveScope("direct")

    async def build_receive(
        self,
        event: Event,
        *,
        bot: BotClient,
        services: ApplicationServices,
        reply_image: bool,
    ) -> MessageReceive | None:
        scope = self.receive_scope(event)
        content = await self._event_content(event, bot, reply_image)
        if not content:
            return None
        return MessageReceive(
            bot_id=self.receive_bot_id(event),
            bot_self_id=event.bot_id,
            msg_id=event.message_id or "",
            user_type=scope.user_type,
            group_id=scope.group_id,
            user_id=event.user_id,
            sender=self._sender(event, bot),
            user_pm=user_pm(event, services),
            content=content,
        )

    def _sender(self, event: Event, bot: BotClient) -> dict[str, Any]:
        sender: dict[str, Any] = {
            "user_id": event.user_id,
            "nickname": event.user_name,
            "card": event.user_name,
        }
        role = event.metadata.get("sender_role")
        if role in ROLES:
            sender["role"] = role
        if Capability.AVATAR in bot.capabilities:
            try:
                sender["avatar"] = bot.capabilities.require(Capability.AVATAR).avatar_url(event.user_id)
            except (ValueError, UnsupportedCapability):
                pass
        return sender

    async def _event_content(self, event: Event, bot: BotClient, reply_image: bool) -> list[WireMessage]:
        content: list[WireMessage] = []
        if reply_image and event.reply is not None:
            content.extend(await self._reply_images(event, bot))
        message = event.original_message if event.original_message is not None else event.message
        stripped = False
        for segment in message.segments:
            kind = segment.type
            if kind == "text":
                text = segment.text_value()
                if not stripped:
                    stripped = True
                    stripped_text = text.strip()
                    for prefix in COMMAND_PREFIXES:
                        if stripped_text.startswith(prefix):
                            text = stripped_text[len(prefix) :]
                            break
                if text:
                    content.append(WireMessage("text", text))
            elif kind == "mention":
                user_id = segment.data.get("user_id")
                if segment.data.get("is_bot"):
                    # 平台对「自己」的标识不统一（官方是 openid），统一成 Bot 自身 ID。
                    user_id = bot.bot_id
                if user_id is not None and str(user_id):
                    content.append(WireMessage("at", str(user_id)))
            elif kind == "reply":
                message_id = segment.data.get("message_id")
                if message_id is not None and str(message_id):
                    content.append(WireMessage("reply", str(message_id)))
            elif kind in {"image", "voice", "audio", "video"}:
                reference = await self._wire_media(segment, bot)
                if reference is not None:
                    content.append(WireMessage("record" if kind in {"voice", "audio"} else kind, reference))
            elif kind == "file":
                reference = await self._wire_file(segment, bot)
                if reference is not None:
                    content.append(WireMessage("file", reference))
            else:
                logger.debug("[gscore] 忽略未知入站消息段: {}", kind)
        return content

    async def _reply_images(self, event: Event, bot: BotClient) -> list[WireMessage]:
        if Capability.REPLY not in bot.capabilities:
            return []
        try:
            quoted = await bot.capabilities.require(Capability.REPLY).get_reply(event)
        except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError) as exc:
            logger.debug("[gscore] 引用消息查询失败，忽略回复图片: {}", exc)
            return []
        if quoted is None:
            return []
        images = []
        for segment in quoted.message.segments:
            if segment.type != "image":
                continue
            reference = await self._wire_media(segment, bot)
            if reference is not None:
                images.append(WireMessage("image", reference))
        return images

    async def _wire_media(self, segment: MessageSegment, bot: BotClient) -> str | None:
        reference = wire_media_reference(media_value(segment.data))
        if reference is not None:
            return reference
        if Capability.MEDIA not in bot.capabilities:
            return None
        try:
            resolved = await bot.capabilities.require(Capability.MEDIA).resolve_media(segment)
        except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError) as exc:
            logger.debug("[gscore] 媒体解析失败，忽略该段: {}", exc)
            return None
        return wire_media_reference(resolved)

    async def _wire_file(self, segment: MessageSegment, bot: BotClient) -> str | None:
        data = segment.data if isinstance(segment.data, dict) else {}
        name = data.get("name") or data.get("file_name") or "file"
        reference = wire_file_reference(media_value(data))
        if reference is None and Capability.MEDIA in bot.capabilities:
            try:
                reference = wire_file_reference(await bot.capabilities.require(Capability.MEDIA).resolve_media(segment))
            except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError) as exc:
                logger.debug("[gscore] 文件解析失败，忽略该段: {}", exc)
                return None
        if reference is None:
            return None
        return f"{name}|{reference}"

    def build_meta(self, event: Event) -> MetaEvent | None:
        if event.event_type in META_NAMES:
            scope = self.receive_scope(event)
            data: dict[str, Any] = {"user_id": event.user_id}
            if scope.group_id:
                data["group_id"] = scope.group_id
            operator_id = event.metadata.get("operator_id")
            if operator_id is not None:
                data["operator_id"] = str(operator_id)
            sub_type = event.metadata.get("sub_type")
            if isinstance(sub_type, str) and sub_type:
                data["sub_type"] = sub_type
            return MetaEvent(META_NAMES[event.event_type], data)
        if event.event_type == "notification":
            notice = event.metadata.get("notice_type")
            sub_type = event.metadata.get("sub_type")
            if notice == "notify" and sub_type == "poke":
                data = {"user_id": event.user_id}
                target_id = event.metadata.get("target_id")
                if target_id is not None:
                    data["target_id"] = str(target_id)
                scope = self.receive_scope(event)
                if scope.group_id:
                    data["group_id"] = scope.group_id
                return MetaEvent("poke", data)
        return None

    def select_bots(self, bots: tuple[BotClient, ...], msg: MessageSend) -> list[BotClient]:
        candidates = [bot for bot in bots if self.platform_matches(bot.platform)]
        if msg.bot_self_id:
            exact = [bot for bot in candidates if bot.bot_id == msg.bot_self_id]
            if exact:
                return exact[:1]
            logger.warning("[gscore] 指定的 bot_self_id 不在线: {}", msg.bot_self_id)
            return []
        if not candidates:
            return []
        if len(candidates) > 1:
            logger.warning("[gscore] 未指定 bot_self_id，使用第一个在线 Bot: {}", candidates[0].bot_id)
        return candidates[:1]

    def target(self, bot: BotClient, msg: MessageSend) -> SessionTarget | None:
        if msg.target_id is None:
            return None
        if msg.target_type == "group":
            kind = "group"
        elif msg.target_type in {None, "direct"}:
            kind = "private"
        else:
            return None
        return SessionTarget(bot.platform, bot.bot_id, kind, str(msg.target_id))

    def check_passive(
        self, bot: BotClient, target: SessionTarget, msg: MessageSend, services: ApplicationServices
    ) -> None:
        """平台可按需记录无法主动指定的被动消息限制。"""

    async def apply_controls(self, bot: BotClient, target: SessionTarget, msg: MessageSend) -> None:
        """撤回与禁言是控制段，不作为普通消息发送。"""
        for segment in msg.content or []:
            data = segment.data if isinstance(segment.data, dict) else {}
            if segment.type == "excute_delete_message":
                message_id = data.get("message_id")
                if message_id is None:
                    logger.debug("[gscore] 撤回控制包缺少 message_id")
                    continue
                try:
                    await bot.capabilities.require(Capability.RECALL).delete_message(str(message_id))
                except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError) as exc:
                    logger.warning("[gscore] 撤回失败: {}", exc)
            elif segment.type == "excute_ban_user":
                user_id, group_id, duration = data.get("user_id"), data.get("group_id"), data.get("duration")
                if user_id is None or group_id is None:
                    logger.debug("[gscore] 禁言控制包缺少 user_id/group_id")
                    continue
                if isinstance(duration, bool) or not (
                    isinstance(duration, int) or isinstance(duration, str) and duration.isdigit()
                ):
                    logger.debug("[gscore] 禁言控制包 duration 无效")
                    continue
                seconds = int(duration)
                if seconds < 0:
                    logger.debug("[gscore] 禁言控制包 duration 不能为负")
                    continue
                try:
                    await bot.capabilities.require(Capability.MEMBER_MODERATION).ban(
                        str(group_id), str(user_id), seconds
                    )
                except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError) as exc:
                    logger.warning("[gscore] 禁言失败: {}", exc)

    def frames(self, content: list[WireMessage]) -> list[list[WireMessage]]:
        """按出现顺序拆分普通消息与 node；node 每一项展开为独立消息。"""
        frames: list[list[WireMessage]] = []
        current: list[WireMessage] = []
        for segment in content:
            if segment.type in CONTROL_TYPES or segment.type in MARKER_TYPES:
                continue
            if segment.type == "node":
                if current:
                    frames.append(current)
                    current = []
                if not isinstance(segment.data, list):
                    logger.debug("[gscore] node 数据不是数组，已跳过")
                    continue
                for item in segment.data:
                    converted = _node_item(item)
                    if converted is not None:
                        frames.append([converted])
            else:
                current.append(segment)
        if current:
            frames.append(current)
        return frames

    async def prepare_frames(
        self, bot: BotClient, target: SessionTarget, msg: MessageSend, frames: list[list[WireMessage]]
    ) -> list[list[WireMessage]]:
        """平台可在发送前调整帧分组，默认保持原样。"""
        return frames

    def build_message(
        self, bot: BotClient, target: SessionTarget, msg: MessageSend, frame: list[WireMessage]
    ) -> CoreMessage | None:
        segments: list[MessageSegment] = []
        for segment in frame:
            kind, data = segment.type, segment.data
            if kind == "text":
                text = data if isinstance(data, str) else "" if data is None else str(data)
                if text:
                    segments.append(MessageSegment.text(text))
            elif kind == "at":
                user_id = data.get("user_id") if isinstance(data, dict) else data
                if user_id is not None and str(user_id):
                    segments.append(MessageSegment.mention(str(user_id)))
            elif kind == "reply":
                message_id = data.get("message_id") if isinstance(data, dict) else data
                if message_id is not None and str(message_id):
                    segments.append(MessageSegment.reply(str(message_id)))
            elif kind in {"image", "record", "video"}:
                source = outbound_media(data)
                if source is None:
                    logger.debug("[gscore] 无法解析下行媒体来源: {}", kind)
                    continue
                if kind == "image":
                    segments.append(MessageSegment.image(source))
                elif kind == "video":
                    segments.append(MessageSegment.video(source))
                else:
                    segments.append(MessageSegment.voice(source))
            elif kind == "file":
                parsed = outbound_file(data)
                if parsed is None:
                    logger.debug("[gscore] 无法解析下行文件段")
                    continue
                name, source = parsed
                segments.append(MessageSegment("file", {"file": source, "name": name}))
            elif kind == "group":
                continue
            elif kind in {"markdown", "template_markdown", "template_buttons", "buttons"}:
                logger.debug("[gscore] 当前平台不支持高级消息段: {}", kind)
            else:
                logger.debug("[gscore] 当前平台不支持下行消息段: {}", kind)
        return CoreMessage(segments)

    async def send_files(self, bot: BotClient, target: SessionTarget, msg: MessageSend) -> None:
        """仅 OneBot V11 等需要独立上传动作的平台覆盖此钩子。"""

    async def send(self, services: ApplicationServices, msg: MessageSend) -> SendResult:
        if not msg.content:
            logger.debug("[gscore] 下行帧没有内容: {}", msg.bot_id)
            return SendResult()
        bots = self.select_bots(services.application.bots, msg)
        if not bots:
            logger.warning("[gscore] 未找到可用的在线 Bot: bot_id={} bot_self_id={}", msg.bot_id, msg.bot_self_id)
            return SendResult()
        ids: list[str] = []
        for bot in bots:
            target = self.target(bot, msg)
            if target is None:
                logger.warning("[gscore] 无法解析发送目标: {} {}", msg.target_type, msg.target_id)
                continue
            self.check_passive(bot, target, msg, services)
            await self.apply_controls(bot, target, msg)
            frames = await self.prepare_frames(bot, target, msg, self.frames(msg.content))
            for frame in frames:
                message = self.build_message(bot, target, msg, frame)
                if message is None or not message.segments:
                    continue
                try:
                    result = await bot.send(target, message)
                except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError) as exc:
                    logger.warning("[gscore] 发送失败 {} {}: {}", target.kind, target.target_id, exc)
                    continue
                ids.extend(extract_message_ids(result))
            await self.send_files(bot, target, msg)
        return SendResult(combine_ids(ids))


def write_temp_file(name: str, data: bytes) -> Path:
    """文件上传能力需要本机路径时，写入插件缓存并立即返回路径。"""
    suffix = Path(name).suffix[:16]
    path = cache_dir("gscore") / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(data)
    return path
