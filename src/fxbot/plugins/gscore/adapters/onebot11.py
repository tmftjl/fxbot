"""OneBot V11 适配器：bot_id 固定为 ``onebot``。"""

from __future__ import annotations

from typing import Any
from dataclasses import replace

from loguru import logger

from fxbot.core import (
    Event,
    Message,
    BotClient,
    Capability,
    SessionTarget,
    ApplicationServices,
    UnsupportedCapability,
)
from fxbot.core.errors import ActionFailed

from .base import (
    SendResult,
    CoreAdapter,
    _node_item,
    combine_ids,
    outbound_file,
    write_temp_file,
    extract_message_ids,
)
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

    async def send(self, services: ApplicationServices, msg: MessageSend) -> SendResult:
        """node 先发合并转发，普通内容最后发一条消息。"""
        content = msg.content or []
        nodes = [segment for segment in content if segment.type == "node"]
        if not nodes:
            return await super().send(services, msg)
        normal = [segment for segment in content if segment.type != "node"]
        ids: list[str] = []
        for bot in self.select_bots(services.application.bots, msg):
            target = self.target(bot, msg)
            if target is None:
                logger.warning("[gscore] 无法解析发送目标: {} {}", msg.target_type, msg.target_id)
                continue
            self.check_passive(bot, target, msg, services)
            for node in nodes:
                ids.extend(await self._send_node(bot, target, msg, node.data))
        if normal:
            result = await super().send(services, replace(msg, content=normal))
            if isinstance(result.recall_id, list):
                ids.extend(result.recall_id)
            elif result.recall_id is not None:
                ids.append(result.recall_id)
        return SendResult(combine_ids(ids))

    async def _send_node(self, bot: BotClient, target: SessionTarget, msg: MessageSend, data: Any) -> list[str]:
        """把 gscore node 作为一条合并转发发送。"""
        if Capability.FORWARD_SEND not in bot.capabilities or not isinstance(data, list):
            logger.warning("[gscore] OneBot V11 连接不支持合并转发，node 已跳过")
            return []
        nodes: list[Message] = []
        for item in data:
            wire = _node_item(item)
            if wire is None:
                continue
            message = self.build_message(bot, target, msg, [wire])
            if message is not None and message.segments:
                nodes.append(message)
        if not nodes:
            return []
        try:
            result = await bot.capabilities.require(Capability.FORWARD_SEND).send_forward(target, nodes)
        except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError) as exc:
            logger.warning("[gscore] OneBot V11 合并转发失败: {}", exc)
            return []
        return extract_message_ids(result)

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
