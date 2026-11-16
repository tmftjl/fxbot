"""OneBot V11 扩展动作：历史、精华、禁言成员和历史分页方言探测。"""

from __future__ import annotations

import time
import asyncio
from typing import Any, Literal

from .codec import _id, parse_message
from ...core import Capability, SessionTarget, HistoryMessage, UnsupportedCapability
from .client import ActionSender, FileUploader, OneBot11Client
from ...core.errors import ActionFailed

DIALECT_TIMEOUT = 3.0
DIALECT_TTL = 600.0
PAGE_SIZE = 100
MAX_PAGES = 100
HistoryDialect = Literal["napcat", "sl"]
_PLACEHOLDERS = {
    "image": "[图片]",
    "onebot11:face": "[表情]",
    "onebot11:mface": "[表情]",
    "voice": "[语音]",
    "video": "[视频]",
    "forward": "[转发]",
    "onebot11:json": "[卡片]",
    "onebot11:xml": "[卡片]",
    "mention": "@",
    "mention_all": "@全体成员",
    "reply": "",
}


def history_message(raw: dict[str, Any], target: SessionTarget) -> HistoryMessage:
    """按 NapCat 的 OB11Message 结构验证身份和内容。"""
    if not isinstance(raw, dict):
        raise ValueError("历史消息必须为对象")
    if raw["message_type"] != "group" or _id(raw["group_id"]) != target.target_id:
        raise ValueError("历史消息不属于请求的群")
    if "self_id" in raw and _id(raw["self_id"]) != target.bot_id:
        raise ValueError("历史消息不属于当前 Bot")
    timestamp = raw["time"]
    if type(timestamp) is not int or not 0 <= timestamp <= 253402300799:
        raise ValueError("历史消息时间无效")
    user_id = _id(raw["user_id"])
    sender = raw["sender"]
    if not isinstance(sender, dict):
        raise ValueError("历史消息发送者必须为对象")
    if "user_id" in sender and _id(sender["user_id"]) != user_id:
        raise ValueError("历史消息发送者身份不一致")
    name = sender.get("card") or sender.get("nickname") or ""
    if not isinstance(name, str):
        raise ValueError("历史消息昵称必须是文本")
    message = parse_message(raw["message"])
    text = "".join(
        segment.text_value()
        if segment.type == "text"
        else _PLACEHOLDERS.get(segment.type, f"[消息类型:{segment.type}]")
        for segment in message.segments
    )
    reply = next((segment.data["message_id"] for segment in message.segments if segment.type == "reply"), "")
    return HistoryMessage(
        _id(raw["message_id"], positive=False),
        timestamp,
        user_id,
        name,
        text,
        sum(segment.type == "image" for segment in message.segments),
        reply,
    )


def _history_params(group_id: str, cursor: int | str | None, dialect: HistoryDialect) -> dict[str, Any]:
    """NapCat 使用 message_seq，SnowLuma 使用 message_id。"""
    params: dict[str, Any] = {"group_id": int(_id(group_id)), "count": PAGE_SIZE}
    if dialect == "sl":
        params["reverse_order"] = True
        if cursor is not None:
            params["message_id"] = cursor
        return params
    params["reverseOrder"] = True
    if cursor is not None:
        params["message_seq"] = cursor
    return params


def _history_anchor(raw: dict[str, Any], dialect: HistoryDialect) -> int:
    """读取当前方言的精确分页字段，不用其他 ID 猜测。"""
    return int(_id(raw["message_id" if dialect == "sl" else "message_seq"], positive=False))


def _history_page(result: Any, target: SessionTarget, dialect: HistoryDialect) -> list[tuple[HistoryMessage, int]]:
    """只在上游响应解析边界转换协议错误，不改变调用参数错误的语义。"""
    try:
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            raise ValueError("历史查询必须返回 messages 数组")
        parsed = [(history_message(raw, target), _history_anchor(raw, dialect)) for raw in result["messages"]]
    except (KeyError, TypeError, ValueError) as error:
        raise ActionFailed("OneBot 历史响应结构或身份无效") from error
    # 查询方向不等于数组顺序；稳定排序保留同秒消息的协议顺序，不拿短 ID 猜先后。
    return sorted(parsed, key=lambda item: item[0].timestamp)


class OneBot11ExtendedClient(OneBot11Client):
    """提供受实现识别保护的扩展接口，普通消息无需识别。"""

    def __init__(
        self,
        bot_id: str,
        sender: ActionSender,
        *,
        file_uploader: FileUploader | None = None,
    ) -> None:
        super().__init__(bot_id, sender, file_uploader=file_uploader)
        self._dialect: HistoryDialect | None = None
        self._dialect_expires = 0.0
        self._dialect_lock = asyncio.Lock()
        self.capabilities = (
            self.capabilities.provide(Capability.HISTORY, self)
            .provide(Capability.ESSENCE, self)
            .provide(Capability.MUTED_MEMBERS, self)
        )

    async def extension_dialect(self) -> HistoryDialect:
        """连接内串行识别已支持的实现，只缓存成功结果，不猜测失败结果。"""
        async with self._dialect_lock:
            if self._dialect is not None and self._dialect_expires > time.monotonic():
                return self._dialect
            info = await asyncio.wait_for(self.call("get_version_info"), timeout=DIALECT_TIMEOUT)
            if not isinstance(info, dict) or not isinstance(info.get("app_name"), str):
                raise ActionFailed("get_version_info 必须返回非空 app_name 字符串")
            name = info["app_name"].strip().lower()
            if not name:
                raise ActionFailed("get_version_info 必须返回非空 app_name 字符串")
            if name == "napcat" or name.startswith("napcat."):
                dialect: HistoryDialect = "napcat"
            elif name in {"sl", "snowluma"} or name.startswith("snowluma."):
                dialect = "sl"
            else:
                raise UnsupportedCapability(f"尚未支持此 OneBot V11 实现的扩展动作: {name}")
            self._dialect = dialect
            self._dialect_expires = time.monotonic() + DIALECT_TTL
            return dialect

    async def set_essence(self, message_id: str, *, enabled: bool) -> Any:
        identifier = int(_id(message_id, positive=False))
        await self.extension_dialect()
        return await self.call("set_essence_msg" if enabled else "delete_essence_msg", message_id=identifier)

    async def get_muted_members(self, group_id: str) -> list[dict[str, Any]]:
        _id(group_id)
        await self.extension_dialect()
        now = int(time.time())
        result = []
        for member in await self.get_members(group_id):
            until = member["shut_up_timestamp"]
            if type(until) is not int or until < 0:
                raise ValueError("禁言到期时间无效")
            if until > now:
                result.append(
                    {
                        "user_id": _id(member["user_id"]),
                        "nickname": member["card"] or member["nickname"],
                        "role": member["role"],
                        "remaining": until - now,
                        "mute_until": until,
                    }
                )
        return result

    async def fetch_group_history(
        self, target: SessionTarget, *, start_time: int, end_time: int, limit: int
    ) -> list[HistoryMessage]:
        if (target.platform, target.bot_id, target.kind) != (self.platform, self.bot_id, "group"):
            raise ValueError("历史查询目标不属于当前群连接")
        if (
            type(start_time) is not int
            or type(end_time) is not int
            or not 0 <= start_time < end_time
            or type(limit) is not int
            or not 1 <= limit <= 5000
        ):
            raise ValueError("历史查询必须给出有效时间窗口和 1 到 5000 的条数上限")
        dialect = await self.extension_dialect()
        cursor = None
        anchors = set()
        collected: dict[str, HistoryMessage] = {}
        for _ in range(MAX_PAGES):
            result = await self.call("get_group_msg_history", **_history_params(target.target_id, cursor, dialect))
            parsed = _history_page(result, target, dialect)
            if not parsed:
                break
            for message, _ in parsed:
                if start_time <= message.timestamp < end_time and (message.text or message.images):
                    collected.setdefault(message.message_id, message)
            oldest, anchor = parsed[0]
            if len(collected) >= limit or oldest.timestamp < start_time:
                break
            if anchor in anchors:
                if len(parsed) == 1 and anchor == cursor:
                    break
                raise ActionFailed("历史分页锚点未前进，不能返回不完整的统计结果")
            anchors.add(anchor)
            cursor = anchor
        else:
            raise ActionFailed("历史查询超过分页预算，不能返回不完整的统计结果")
        return sorted(collected.values(), key=lambda message: message.timestamp)[-limit:]
