"""NapCat 明确声明的群历史查询，不探测版本或切换请求方言。"""

from __future__ import annotations

import time
from typing import Any

from .codec import _id, parse_message
from ...core import Capability, SessionTarget, HistoryMessage
from .client import OneBot11Client

PAGE_SIZE = 100
MAX_PAGES = 100
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


class NapCatClient(OneBot11Client):
    """明确选中 NapCat 后才授予历史读取能力。"""

    capabilities = OneBot11Client.capabilities | {Capability.HISTORY, Capability.ESSENCE, Capability.MUTED_MEMBERS}

    async def set_essence(self, message_id: str, *, enabled: bool) -> Any:
        return await self.call(
            "set_essence_msg" if enabled else "delete_essence_msg", message_id=int(_id(message_id, positive=False))
        )

    async def get_muted_members(self, group_id: str) -> list[dict[str, Any]]:
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
        cursor = None
        anchors = set()
        collected: dict[str, HistoryMessage] = {}
        for _ in range(MAX_PAGES):
            params = {"group_id": _id(target.target_id), "count": PAGE_SIZE, "reverse_order": True}
            if cursor is not None:
                params["message_seq"] = cursor
            result = await self.call("get_group_msg_history", **params)
            page = result["messages"]
            if not isinstance(page, list):
                raise ValueError("历史查询必须返回 messages 数组")
            if not page:
                break
            parsed = []
            for raw in page:
                message = history_message(raw, target)
                sequence = raw["message_seq"]
                if type(sequence) is not int or sequence < 0:
                    raise ValueError("历史消息必须包含明确的消息序号")
                parsed.append((message, sequence))
                if start_time <= message.timestamp < end_time and (message.text or message.images):
                    collected.setdefault(message.message_id, message)
            oldest, _ = min(parsed, key=lambda item: (item[0].timestamp, item[1]))
            if len(collected) >= limit or oldest.timestamp < start_time:
                break
            # 请求参数接收已返回的短消息 ID，由 NapCat 在内部解析为 MsgId。
            cursor = oldest.message_id
            if cursor in anchors:
                break
            anchors.add(cursor)
        else:
            raise ValueError("历史查询超过分页预算，不能返回不完整的统计结果")
        return sorted(collected.values(), key=lambda message: message.timestamp)[-limit:]
