"""群违禁词的群号存储和原生拦截。"""

from __future__ import annotations

import re
import json

from loguru import logger

from fxbot.core.errors import ActionFailed

from ...core import Event, SessionTarget, HandlerContext, UnsupportedCapability
from .service import GroupAdminService
from ...utils.tz import now_str
from ...core.plugin import on_regex, on_message
from ...utils.paths import data_dir
from ...utils.json_file import read_json, write_json


class Banwords:
    def __init__(self, service: GroupAdminService) -> None:
        self.service = service
        self.directory = data_dir("group_admin") / "banned_words"
        definitions = (
            ("banword_add", r"^[#＃]新增(精确|模糊|正则)?(禁|踢|撤)?违禁词\s*(.+)"),
            ("banword_del", r"^[#＃]删除违禁词\s*(.+)"),
            ("banword_clear", r"^[#＃]清空违禁词$"),
            ("banword_list", r"^[#＃]违禁词列表$"),
            ("banword_on", r"^[#＃]开启违禁词$"),
            ("banword_off", r"^[#＃]关闭违禁词$"),
            ("banword_mute_time", r"^[#＃]设置违禁词禁言时间\s*(\d+)$"),
        )
        self.patterns = {name: re.compile(pattern) for name, pattern in definitions}

    @staticmethod
    def _group_id(target: SessionTarget) -> str:
        if target.kind != "group":
            raise ValueError("违禁词只能配置群会话")
        value = target.target_id
        if not value or ":" in value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("违禁词群号无效")
        return value

    def path(self, target: SessionTarget):
        return self.directory / f"{self._group_id(target)}.json"

    @staticmethod
    def _default() -> dict:
        return {"banned_words": {}, "config": {"enabled": True, "mute_seconds": 300}}

    def _validate(self, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) != {"banned_words", "config"}:
            raise ValueError("违禁词配置格式无效")
        config = data["config"]
        if (
            not isinstance(config, dict)
            or set(config) != {"enabled", "mute_seconds"}
            or type(config["enabled"]) is not bool
            or type(config["mute_seconds"]) is not int
            or not 1 <= config["mute_seconds"] <= 2592000
        ):
            raise ValueError("违禁词开关或禁言秒数无效")
        words = data["banned_words"]
        if not isinstance(words, dict):
            raise ValueError("违禁词必须为对象")
        for word, item in words.items():
            if not isinstance(word, str) or not word or not isinstance(item, dict):
                raise ValueError("违禁词记录无效")
            if not {"match_type", "penalty_type"} <= set(item) or set(item) - {
                "match_type",
                "penalty_type",
                "added_by",
                "added_at",
            }:
                raise ValueError("违禁词记录字段无效")
            if item["match_type"] not in {"exact", "fuzzy", "regex"} or item["penalty_type"] not in {
                "mute",
                "kick",
                "recall",
            }:
                raise ValueError("违禁词匹配或处罚类型无效")
            if item["match_type"] == "regex":
                re.compile(word, re.MULTILINE)
        return data

    def validate(self) -> None:
        if not self.directory.exists():
            return
        for path in self.directory.glob("*.json"):
            try:
                self._group_id(SessionTarget("onebot11", "0", "group", path.stem))
                self._validate(read_json(path, dict))
            except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning("[banwords] 跳过无效配置 {}: {}", path.name, exc)

    def load(self, target: SessionTarget) -> dict:
        path = self.path(target)
        try:
            data = read_json(path, self._default)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("[banwords] 配置读取失败 {}: {}", path.name, exc)
            return self._default()
        if not isinstance(data, dict):
            return self._default()
        try:
            return self._validate(data)
        except ValueError as exc:
            logger.warning("[banwords] 配置无效 {}: {}", path.name, exc)
            return self._default()

    async def handle(self, action: str, context: HandlerContext) -> None:
        target = context.event.target
        data = self.load(target)
        words = data["banned_words"]
        config = data["config"]
        match = self.patterns[action].search(context.arguments)
        if action == "banword_add":
            mode = {"精确": "exact", "模糊": "fuzzy", "正则": "regex"}[match.group(1) or "精确"]
            penalty = {"禁": "mute", "踢": "kick", "撤": "recall"}[match.group(2) or "禁"]
            word = match.group(3).strip()
            if not word:
                await context.finish("请提供违禁词")
            if word in words:
                await context.finish("违禁词已经存在")
            if mode == "regex":
                try:
                    re.compile(word, re.MULTILINE)
                except re.error:
                    await context.finish("正则表达式无效")
            words[word] = {
                "match_type": mode,
                "penalty_type": penalty,
                "added_by": context.event.user_id,
                "added_at": now_str(),
            }
        elif action == "banword_del":
            if words.pop(match.group(1).strip(), None) is None:
                await context.finish("违禁词不存在")
        elif action == "banword_clear":
            data["banned_words"] = {}
        elif action == "banword_on":
            config["enabled"] = True
        elif action == "banword_off":
            config["enabled"] = False
        elif action == "banword_mute_time":
            config["mute_seconds"] = int(match.group(1))
            if not 1 <= config["mute_seconds"] <= 2592000:
                await context.finish("禁言时间必须在 1 到 2592000 秒之间")
        else:
            await context.finish(
                "\n".join(
                    [
                        f"违禁词检测：{'开启' if config['enabled'] else '关闭'}，禁言 {config['mute_seconds']} 秒",
                        *[
                            f"{word} [{item['match_type']}/{item['penalty_type']}]"
                            for word, item in list(data["banned_words"].items())[:50]
                        ],
                    ]
                )
            )
        write_json(self.path(target), data)
        await context.finish("违禁词配置已保存")

    async def rule(self, event: Event) -> bool:
        if not event.is_group or not await self.service.authorizer.check("group_admin:banword_interceptor", event):
            return False
        if event.metadata.get("sender_role") in {"owner", "admin"} or self.service.protected(
            event.target, event.user_id
        ):
            return False
        data = self.load(event.target)
        config = data["config"]
        if not config["enabled"]:
            return False
        text = event.message.plain_text()
        compact = re.sub(r"\s+", "", text)
        for word, item in data["banned_words"].items():
            match_type = item["match_type"]
            if match_type == "exact":
                matched = text == word or compact == word
            elif match_type == "fuzzy":
                matched = word in text or word in compact
            else:
                pattern = re.compile(word, re.MULTILINE)
                matched = pattern.search(text) is not None or pattern.search(compact) is not None
            if matched:
                event.metadata["group_admin:banword"] = (item["penalty_type"], config["mute_seconds"])
                return True
        return False

    async def intercept(self, context: HandlerContext) -> None:
        penalty, duration = context.event.metadata.pop("group_admin:banword")
        try:
            await self.service.automatic(
                context.bot,
                context.event.target,
                context.event.user_id,
                penalty,
                duration=duration,
                message_id=context.event.message_id or "",
            )
        except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError):
            logger.opt(exception=True).warning("违禁词命中，但无法确认安全处罚条件")


banword_add_matcher = on_regex(
    r"^[#＃]新增(精确|模糊|正则)?(禁|踢|撤)?违禁词\s*(.+)",
    name="banword_add",
    display_name="新增违禁词",
    level="admin",
    scene="group",
)


@banword_add_matcher.handle()
async def banword_add(context: HandlerContext) -> None:
    await banwords(context.app_services).handle("banword_add", context)


banword_del_matcher = on_regex(
    r"^[#＃]删除违禁词\s*(.+)", name="banword_del", display_name="删除违禁词", level="admin", scene="group"
)


@banword_del_matcher.handle()
async def banword_del(context: HandlerContext) -> None:
    await banwords(context.app_services).handle("banword_del", context)


banword_clear_matcher = on_regex(
    r"^[#＃]清空违禁词$", name="banword_clear", display_name="清空违禁词", level="admin", scene="group"
)


@banword_clear_matcher.handle()
async def banword_clear(context: HandlerContext) -> None:
    await banwords(context.app_services).handle("banword_clear", context)


banword_list_matcher = on_regex(
    r"^[#＃]违禁词列表$", name="banword_list", display_name="违禁词列表", level="admin", scene="group"
)


@banword_list_matcher.handle()
async def banword_list(context: HandlerContext) -> None:
    await banwords(context.app_services).handle("banword_list", context)


banword_on_matcher = on_regex(
    r"^[#＃]开启违禁词$", name="banword_on", display_name="开启违禁词", level="admin", scene="group"
)


@banword_on_matcher.handle()
async def banword_on(context: HandlerContext) -> None:
    await banwords(context.app_services).handle("banword_on", context)


banword_off_matcher = on_regex(
    r"^[#＃]关闭违禁词$", name="banword_off", display_name="关闭违禁词", level="admin", scene="group"
)


@banword_off_matcher.handle()
async def banword_off(context: HandlerContext) -> None:
    await banwords(context.app_services).handle("banword_off", context)


banword_mute_time_matcher = on_regex(
    r"^[#＃]设置违禁词禁言时间\s*(\d+)$",
    name="banword_mute_time",
    display_name="违禁词禁言时长",
    level="admin",
    scene="group",
)


@banword_mute_time_matcher.handle()
async def banword_mute_time(context: HandlerContext) -> None:
    await banwords(context.app_services).handle("banword_mute_time", context)


async def matches_banword(context):
    return await banwords(context.app_services).rule(context.event)


intercept_matcher = on_message(
    name="banword_interceptor", display_name="违禁词拦截", scene="group", priority=0, rule=matches_banword
)


@intercept_matcher.handle()
async def intercept(context):
    await banwords(context.app_services).intercept(context)


def banwords(runtime):
    service = Banwords(GroupAdminService(runtime.application, runtime.authorizer))
    service.validate()
    return service
