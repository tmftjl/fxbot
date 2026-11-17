"""OneBot V11 适配器：bot_id 固定为 ``onebot``。"""

from __future__ import annotations

from loguru import logger

from fxbot.core import Event, BotClient, Capability, SessionTarget, UnsupportedCapability
from fxbot.core.errors import ActionFailed

from .base import CoreAdapter, outbound_file, write_temp_file
from ..models import MessageSend


class OneBot11Adapter(CoreAdapter):
    key = "onebot11"
    name = "OneBot V11"

    def platform_matches(self, platform: str) -> bool:
        return platform == "onebot11"

    def matches_send(self, bot_id: str) -> bool:
        return bot_id == "onebot"

    def receive_bot_id(self, event: Event) -> str:
        return "onebot"

    async def send_files(self, bot: BotClient, target: SessionTarget, msg: MessageSend) -> None:
        """V11 标准没有文件消息段，只有显式注入的上传能力才能发送。"""
        files = [segment for segment in msg.content or [] if segment.type == "file"]
        if not files:
            return
        if Capability.FILE not in bot.capabilities:
            logger.warning("[gscore] OneBot V11 连接未提供文件上传能力，文件段已跳过")
            return
        for segment in files:
            parsed = outbound_file(segment.data)
            if parsed is None:
                logger.debug("[gscore] 无法解析 OneBot V11 文件段")
                continue
            name, source = parsed
            if isinstance(source, str):
                logger.warning("[gscore] OneBot V11 文件上传暂不支持远程 URL，已跳过: {}", name)
                continue
            path = write_temp_file(name, source)
            try:
                await bot.capabilities.require(Capability.FILE).send_file(target, path, name)
            except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError) as exc:
                logger.warning("[gscore] OneBot V11 文件发送失败: {}", exc)
            finally:
                path.unlink(missing_ok=True)
