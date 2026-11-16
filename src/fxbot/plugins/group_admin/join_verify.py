"""入群验证码的精确身份、限时状态及原生处理。"""

from __future__ import annotations

import io
import time
import random
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont
from loguru import logger

from fxbot.core.errors import ActionFailed

from ...core import Message, BotClient, Capability, SessionTarget, HandlerContext, MessageSegment, UnsupportedCapability
from .service import GroupAdminService
from ...core.plugin import Service, on_event, on_regex, scheduled, on_message
from ...utils.paths import data_dir, package_root
from ...utils.json_file import read_json, write_json

_TIMEOUT = 180
_ATTEMPTS = 2
_DIGITS = "23456789"
_FONT_PATH = package_root() / "resources/fonts/MSGothic.ttc"
_fonts = {}
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _font(size: int) -> ImageFont.FreeTypeFont:
    """按字号缓存字体。"""
    font = _fonts.get(size)
    if font is None:
        font = ImageFont.truetype(str(_FONT_PATH), size)
        _fonts[size] = font
    return font


def _render_captcha(code: str) -> bytes:
    """把验证码画成 PNG：逐字旋转，叠加噪点和横穿折线。"""
    rng = random.SystemRandom()
    width, height = 160, 60
    image = Image.new("RGB", (width, height), (246, 247, 249))
    draw = ImageDraw.Draw(image)
    for _ in range(140):
        gray = rng.randrange(150, 225)
        draw.point((rng.randrange(width), rng.randrange(height)), fill=(gray, gray, gray))
    slot = width // len(code)
    for index, char in enumerate(code):
        layer = Image.new("RGBA", (slot + 12, height), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(
            (6, 4),
            char,
            font=_font(rng.randrange(36, 46)),
            fill=(rng.randrange(0, 105), rng.randrange(0, 105), rng.randrange(0, 105), 255),
        )
        layer = layer.rotate(rng.uniform(-26, 26), resample=Image.BICUBIC)
        image.paste(layer, (index * slot - 4, rng.randint(-5, 5)), layer)
    for _ in range(3):
        points = [(x, rng.randrange(height)) for x in range(0, width + 10, 8)]
        draw.line(points, fill=(rng.randrange(120, 185),) * 3, width=rng.choice([1, 2]))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


@dataclass
class Pending:
    target: SessionTarget
    bot: BotClient
    user_id: str
    code: str
    tries: int
    deadline: float


class JoinVerification(Service):
    def __init__(self, service: GroupAdminService) -> None:
        self.service = service
        self.path = data_dir("group_admin") / "join_verify.json"
        self.pending: dict[tuple[str, str], Pending] = {}
        self._joining: dict[tuple[str, str], object] = {}

    def configs(self) -> dict:
        data = read_json(self.path, dict)
        if not isinstance(data, dict):
            raise ValueError("入群验证配置必须为对象")
        for key, item in data.items():
            if (
                SessionTarget.from_key(key).kind != "group"
                or not isinstance(item, dict)
                or set(item) != {"enabled"}
                or type(item.get("enabled")) is not bool
            ):
                raise ValueError("入群验证必须明确群身份和布尔开关")
        return data

    def enabled(self, target: SessionTarget) -> bool:
        return self.configs().get(target.key(), {}).get("enabled", False)

    async def toggle(self, context: HandlerContext) -> None:
        data = self.configs()
        enabled = "开启" in context.arguments
        if enabled and Capability.MEMBER_MODERATION not in context.bot.capabilities:
            await context.finish("当前平台不支持自动入群验证")
        data[context.event.target.key()] = {"enabled": enabled}
        write_json(self.path, data)
        if not enabled:
            for key in [key for key in self.pending if key[0] == context.event.target.key()]:
                self.pending.pop(key)
            for key in [key for key in self._joining if key[0] == context.event.target.key()]:
                self._joining.pop(key)
        await context.finish(f"已{'开启' if enabled else '关闭'}进群验证")

    async def joined(self, context: HandlerContext) -> None:
        event, bot = context.event, context.bot
        target = event.target
        key = (target.key(), event.user_id)
        if not self.enabled(target) or event.user_id == bot.bot_id or key in self.pending or key in self._joining:
            return
        if self.service.protected(target, event.user_id):
            return
        ticket = object()
        self._joining[key] = ticket
        try:
            await self._send_challenge(context, key, ticket)
        finally:
            if self._joining.get(key) is ticket:
                self._joining.pop(key)

    async def _send_challenge(self, context: HandlerContext, key: tuple[str, str], ticket: object) -> None:
        event, bot = context.event, context.bot
        target = event.target
        try:
            self.service._online(bot, target)
            own = await bot.capabilities.require(Capability.GROUP_ROLE).member_role(target.target_id, bot.bot_id)
            role = await bot.capabilities.require(Capability.GROUP_ROLE).member_role(target.target_id, event.user_id)
        except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError):
            logger.opt(exception=True).warning("无法确认入群成员身份，本次不执行自动验证")
            return
        if own not in {"admin", "owner"} or role != "member":
            return
        if self._joining.get(key) is not ticket or not self.enabled(target):
            return
        code = "".join(random.SystemRandom().choice(_DIGITS) for _ in range(4))
        image = _render_captcha(code)
        pending = Pending(target, bot, event.user_id, code, 0, time.monotonic() + _TIMEOUT)
        self.pending[key] = pending
        try:
            await context.send(
                Message(
                    [
                        MessageSegment.mention(event.user_id),
                        MessageSegment.text(
                            f" 请在 {_TIMEOUT} 秒内回复图片中的 4 位数字；答错 {_ATTEMPTS} 次或超时将移出。"
                        ),
                        MessageSegment.image(image),
                    ]
                )
            )
        except BaseException:
            if self.pending.get(key) is pending:
                self.pending.pop(key)
            raise

    def has_pending(self, event) -> bool:
        return event.is_group and (event.target.key(), event.user_id) in self.pending

    async def left(self, context: HandlerContext) -> None:
        key = (context.event.target.key(), context.event.user_id)
        self._joining.pop(key, None)
        self.pending.pop(key, None)

    async def _notify(self, record: Pending, text: str) -> None:
        if self.service.application.is_registered(record.bot):
            await record.bot.send(
                record.target,
                Message(
                    [
                        MessageSegment.mention(record.user_id),
                        MessageSegment.text(" " + text),
                    ]
                ),
            )

    async def _failed(self, record: Pending, reason: str) -> None:
        if not self.enabled(record.target) or not self.service.application.is_registered(record.bot):
            return
        try:
            removed = await self.service.automatic_kick(record.bot, record.target, record.user_id)
        except (ActionFailed, UnsupportedCapability, TimeoutError, OSError, ValueError):
            logger.opt(exception=True).warning("入群验证未完成自动移出")
            removed = False
        await self._notify(record, reason + ("，已移出本群" if removed else "，请管理员手动处理"))

    async def answer(self, context: HandlerContext) -> None:
        key = (context.event.target.key(), context.event.user_id)
        record = self.pending.get(key)
        if record is None:
            return
        if record.bot is not context.bot or not self.enabled(record.target):
            self.pending.pop(key)
            return
        if record.deadline <= time.monotonic():
            self.pending.pop(key)
            await self._failed(record, "验证超时")
            return
        answer = "".join(context.event.message.plain_text().translate(_FULLWIDTH_DIGITS).split())
        if answer == record.code:
            self.pending.pop(key)
            await self._notify(record, "验证通过，欢迎加入！")
            return
        if not answer:
            await self._notify(record, "请回复图中 4 位数字")
            return
        record.tries += 1
        if record.tries < _ATTEMPTS:
            await self._notify(record, f"答案不对，还有 {_ATTEMPTS - record.tries} 次机会")
            return
        self.pending.pop(key)
        await self._failed(record, "验证未通过")

    async def sweep(self) -> None:
        for key, record in tuple(self.pending.items()):
            if record.deadline <= time.monotonic() and self.pending.get(key) is record:
                self.pending.pop(key)
                await self._failed(record, "验证超时")

    async def close(self) -> None:
        self._joining.clear()
        self.pending.clear()


toggle_matcher = on_regex(
    r"^[#＃](开启|关闭)进群验证\s*$", name="join_verify", display_name="进群验证开关", level="admin", scene="group"
)


@toggle_matcher.handle()
async def toggle(context):
    await context.service(verification).toggle(context)


joined_matcher = on_event("member_joined", name="join_notice", display_name="入群验证", scene="group", priority=10)


@joined_matcher.handle()
async def joined(context):
    await context.service(verification).joined(context)


left_matcher = on_event("member_left", name="left", scene="group", priority=0)


@left_matcher.handle()
async def left(context):
    await context.service(verification).left(context)


async def has_pending(context):
    return context.service(verification).has_pending(context.event)


answer_matcher = on_message(name="verification_answer", scene="group", priority=1, rule=has_pending)


@answer_matcher.handle()
async def answer(context):
    await context.service(verification).answer(context)


def verification(runtime):
    service = JoinVerification(GroupAdminService(runtime.application, runtime.authorizer))
    service.configs()
    return service


@scheduled(interval=3, name="join_verify")
async def sweep(context):
    await context.service(verification).sweep()
