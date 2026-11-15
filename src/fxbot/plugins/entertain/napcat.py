"""有界点赞服务，不以接口失败探测次数。"""

from __future__ import annotations

import asyncio

from ...utils.tz import today_str
from ...core.plugin import on_command
from ...core.router import HandlerContext
from ...utils.paths import data_dir
from ...utils.json_file import read_json, write_json
from ...core.capabilities import Capability

_lock = asyncio.Lock()


class Likes:
    def __init__(self):
        self.path = data_dir("entertain") / "like_daily.json"
        self.lock = _lock

    def records(self) -> dict:
        data = read_json(self.path, dict)
        if not isinstance(data, dict):
            raise ValueError("点赞记录必须为对象")
        for bot_id, users in data.items():
            if not bot_id.isascii() or not bot_id.isdecimal() or not isinstance(users, dict):
                raise ValueError("点赞记录必须按 Bot QQ 号、用户 QQ 号分层")
            for user_id, record in users.items():
                if (
                    not user_id.isascii()
                    or not user_id.isdecimal()
                    or not isinstance(record, dict)
                    or set(record) != {"date", "count"}
                    or not isinstance(record["date"], str)
                    or type(record["count"]) is not int
                    or record["count"] < 0
                ):
                    raise ValueError("点赞记录需要用户 QQ 号、日期和非负次数")
        return data

    async def send(self, bot, user_id: str, times: int = 10) -> str:
        if Capability.LIKE not in bot.capabilities:
            return "当前平台不支持点赞"
        if type(times) is not int or not 1 <= times <= 10:
            raise ValueError("点赞次数必须为 1 到 10")
        async with self.lock:
            records = self.records()
            today = today_str()
            record = records.get(bot.bot_id, {}).get(user_id)
            if record and record["date"] == today and record["count"] > 0:
                return f"今天已经为你点赞 {record['count']} 次啦，明天再来吧！"
            await bot.capabilities.require(Capability.LIKE).like(user_id, times)
            records.setdefault(bot.bot_id, {})[user_id] = {"date": today, "count": times}
            write_json(self.path, records)
        return f"已为你点赞 {times} 次，记得回我哟！"

    async def handle(self, context: HandlerContext) -> None:
        await context.send(await self.send(context.bot, context.event.user_id))


like_matcher = on_command("赞我", name="like", display_name="QQ点赞", level="all")


@like_matcher.handle()
async def like(context):
    await likes.handle(context)


likes = Likes()
