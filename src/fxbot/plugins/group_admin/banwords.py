"""群违禁词的规范会话存储和原生拦截。"""

from __future__ import annotations

import re
import hashlib

from loguru import logger

from ...core import Event, SessionTarget, HandlerContext, UnsupportedCapability
from .service import GroupAdminService
from ...core.plugin import on_regex, on_message
from ...utils.paths import data_dir
from ...runtime.actions import ActionFailed
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

    def path(self, target: SessionTarget):
        if target.kind != "group":
            raise ValueError("违禁词只能配置群会话")
        return self.directory / (hashlib.sha256(target.key().encode("utf-8")).hexdigest() + ".json")

    def _validate(self, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) != {"target", "enabled", "mute_seconds", "words"}:
            raise ValueError("违禁词配置必须明确包含群会话身份，旧文件不能猜测归属")
        if SessionTarget.from_key(data["target"]).kind != "group" or type(data["enabled"]) is not bool:
            raise ValueError("违禁词目标或开关无效")
        if type(data["mute_seconds"]) is not int or not 1 <= data["mute_seconds"] <= 2592000:
            raise ValueError("违禁词禁言秒数无效")
        if not isinstance(data["words"], dict):
            raise ValueError("违禁词必须为对象")
        for word, item in data["words"].items():
            if not isinstance(word, str) or not word or not isinstance(item, dict):
                raise ValueError("违禁词记录无效")
            if item["match"] not in {"exact", "fuzzy", "regex"} or item["penalty"] not in {"mute", "kick", "recall"}:
                raise ValueError("违禁词匹配或处罚类型无效")
            if item["match"] == "regex":
                re.compile(word, re.MULTILINE)
        return data

    def validate(self) -> None:
        for path in self.directory.glob("*.json"):
            data = self._validate(read_json(path, dict))
            if path != self.path(SessionTarget.from_key(data["target"])):
                raise ValueError("违禁词文件名与完整群身份不一致，必须明确迁移")

    def load(self, target: SessionTarget) -> dict:
        data = read_json(
            self.path(target),
            lambda: {
                "target": target.key(),
                "enabled": True,
                "mute_seconds": 300,
                "words": {},
            },
        )
        data = self._validate(data)
        if data["target"] != target.key():
            raise ValueError("违禁词配置属于其他会话")
        return data

    async def handle(self, action: str, context: HandlerContext) -> None:
        data = self.load(context.event.target)
        match = self.patterns[action].search(context.arguments)
        if action == "banword_add":
            mode = {"精确": "exact", "模糊": "fuzzy", "正则": "regex"}[match.group(1) or "精确"]
            penalty = {"禁": "mute", "踢": "kick", "撤": "recall"}[match.group(2) or "禁"]
            word = match.group(3).strip()
            if not word:
                await context.finish("请提供违禁词")
            if word in data["words"]:
                await context.finish("违禁词已经存在")
            if mode == "regex":
                try:
                    re.compile(word, re.MULTILINE)
                except re.error:
                    await context.finish("正则表达式无效")
            data["words"][word] = {"match": mode, "penalty": penalty, "operator": context.event.user_id}
        elif action == "banword_del":
            if data["words"].pop(match.group(1).strip(), None) is None:
                await context.finish("违禁词不存在")
        elif action == "banword_clear":
            data["words"] = {}
        elif action == "banword_on":
            data["enabled"] = True
        elif action == "banword_off":
            data["enabled"] = False
        elif action == "banword_mute_time":
            data["mute_seconds"] = int(match.group(1))
            if not 1 <= data["mute_seconds"] <= 2592000:
                await context.finish("禁言时间必须在 1 到 2592000 秒之间")
        else:
            await context.finish(
                "\n".join(
                    [
                        f"违禁词检测：{'开启' if data['enabled'] else '关闭'}，禁言 {data['mute_seconds']} 秒",
                        *[
                            f"{word} [{item['match']}/{item['penalty']}]"
                            for word, item in list(data["words"].items())[:50]
                        ],
                    ]
                )
            )
        write_json(self.path(context.event.target), data)
        await context.finish("违禁词配置已保存")

    async def rule(self, event: Event) -> bool:
        if not event.is_group or not await self.service.authorizer.check("group_admin:banword_interceptor", event):
            return False
        if event.metadata.get("sender_role") in {"owner", "admin"} or self.service.protected(
            event.target, event.user_id
        ):
            return False
        data = self.load(event.target)
        if not data["enabled"]:
            return False
        text = event.message.plain_text()
        compact = re.sub(r"\s+", "", text)
        for word, item in data["words"].items():
            if item["match"] == "exact":
                matched = text == word or compact == word
            elif item["match"] == "fuzzy":
                matched = word in text or word in compact
            else:
                pattern = re.compile(word, re.MULTILINE)
                matched = pattern.search(text) is not None or pattern.search(compact) is not None
            if matched:
                event.metadata["group_admin:banword"] = (item["penalty"], data["mute_seconds"])
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


@on_regex(
    r"^[#＃]新增(精确|模糊|正则)?(禁|踢|撤)?违禁词\s*(.+)",
    name="banword_add",
    display_name="新增违禁词",
    level="admin",
    scene="group",
)
async def banword_add(context: HandlerContext) -> None:
    await banwords(context.runtime).handle("banword_add", context)


@on_regex(r"^[#＃]删除违禁词\s*(.+)", name="banword_del", display_name="删除违禁词", level="admin", scene="group")
async def banword_del(context: HandlerContext) -> None:
    await banwords(context.runtime).handle("banword_del", context)


@on_regex(r"^[#＃]清空违禁词$", name="banword_clear", display_name="清空违禁词", level="admin", scene="group")
async def banword_clear(context: HandlerContext) -> None:
    await banwords(context.runtime).handle("banword_clear", context)


@on_regex(r"^[#＃]违禁词列表$", name="banword_list", display_name="违禁词列表", level="admin", scene="group")
async def banword_list(context: HandlerContext) -> None:
    await banwords(context.runtime).handle("banword_list", context)


@on_regex(r"^[#＃]开启违禁词$", name="banword_on", display_name="开启违禁词", level="admin", scene="group")
async def banword_on(context: HandlerContext) -> None:
    await banwords(context.runtime).handle("banword_on", context)


@on_regex(r"^[#＃]关闭违禁词$", name="banword_off", display_name="关闭违禁词", level="admin", scene="group")
async def banword_off(context: HandlerContext) -> None:
    await banwords(context.runtime).handle("banword_off", context)


@on_regex(
    r"^[#＃]设置违禁词禁言时间\s*(\d+)$",
    name="banword_mute_time",
    display_name="违禁词禁言时长",
    level="admin",
    scene="group",
)
async def banword_mute_time(context: HandlerContext) -> None:
    await banwords(context.runtime).handle("banword_mute_time", context)


async def matches_banword(context):
    return await banwords(context.runtime).rule(context.event)


@on_message(name="banword_interceptor", scene="group", priority=0, when=matches_banword)
async def intercept(context):
    await banwords(context.runtime).intercept(context)


def banwords(runtime):
    service = Banwords(GroupAdminService(runtime.application, runtime.authorizer))
    service.validate()
    return service
