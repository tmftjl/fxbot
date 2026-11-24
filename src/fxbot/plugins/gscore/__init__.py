"""gsuid_core（SayuCore）连接器：平台事件上行与 gsuid 下行消息发送。"""

from __future__ import annotations

from loguru import logger

from ...core import HandlerContext, ApplicationServices, UnsupportedCapability
from .client import GscoreService
from .config import get_config
from .inbound import build_receive, build_meta_receive
from .ui_schema import get_ui_schema
from ...core.plugin import PluginMetadata, on_event, scheduled, on_message

__plugin_meta__ = PluginMetadata(
    name="gscore 连接器",
    description="连接 gsuid_core，转发平台事件并发送 gsuid_core 的下行消息。",
    usage="在控制台 gscore 配置页填写 gsuid_core 地址、端口、Token 与连接 Bot ID。",
)

__plugin_extensions__ = {"console.config": get_ui_schema}

CONNECTION_INTERVAL = 10

message_matcher = on_message(priority=0, block=False, name="message", display_name="转发消息")
member_joined = on_event("member_joined", priority=0, name="member_joined", display_name="成员入群")
member_left = on_event("member_left", priority=0, name="member_left", display_name="成员退群")
notification = on_event("notification", priority=0, name="notification", display_name="通知事件")


def build_service(runtime: ApplicationServices) -> GscoreService:
    """连接器服务由应用作用域缓存并在关闭时释放。"""
    return GscoreService(runtime)


@scheduled(interval=CONNECTION_INTERVAL, run_on_start=True, watch_config=True, name="connection")
async def maintain_connection(context) -> None:
    """保持唯一 WS 连接；配置变化时由服务自己重建连接。"""
    await context.service(build_service).maintain(get_config())


@message_matcher.handle()
async def forward_message(context: HandlerContext) -> None:
    config = get_config()
    if not config["enabled"] or context.app_services is None:
        return
    service = context.service(build_service)
    observation = service.mapping.observe(context.event)
    if observation.action == "drop":
        return
    try:
        receive = await build_receive(
            context.event,
            bot=context.bot,
            services=context.app_services,
            reply_image=config["reply_image"],
        )
    except (ValueError, UnsupportedCapability) as exc:
        logger.warning("[gscore] 无法转换消息事件: {}", exc)
        return
    if receive is not None:
        service.mapping.apply_receive(receive, observation)
        service.mapping.remember_receive(receive, observation)
        service.submit(receive)


@member_joined.handle()
@member_left.handle()
@notification.handle()
async def forward_meta(context: HandlerContext) -> None:
    config = get_config()
    if not config["enabled"] or context.app_services is None:
        return
    try:
        receive = build_meta_receive(context.event, services=context.app_services)
    except (ValueError, UnsupportedCapability) as exc:
        logger.warning("[gscore] 无法转换 meta 事件: {}", exc)
        return
    if receive is not None:
        context.service(build_service).submit(receive)
