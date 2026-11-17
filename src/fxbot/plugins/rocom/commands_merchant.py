"""远行商人命令。"""

from __future__ import annotations

from typing import Any

from .store import store as state_store
from ...core import Event, Message, HandlerContext, MessageSegment
from .client import fetch_merchant_snapshot
from .renderer import render_merchant_image
from ...core.plugin import on_regex

merchant_query = on_regex(
    r"^[#＃]远行商人$",
    name="rocom_merchant_query",
    display_name="远行商人查询",
    priority=5,
    block=True,
    level="member",
    scene="all",
)

merchant_subscribe = on_regex(
    r"^(?:#|＃|/)开启远行商人$",
    name="rocom_merchant_subscribe",
    display_name="订阅远行商人",
    priority=5,
    block=True,
    level="admin",
    scene="all",
)

merchant_unsubscribe = on_regex(
    r"^(?:#|＃|/)关闭远行商人$",
    name="rocom_merchant_unsubscribe",
    display_name="取消订阅远行商人",
    priority=5,
    block=True,
    level="admin",
    scene="all",
)

merchant_status = on_regex(
    r"^(?:#|＃|/)远行商人订阅$",
    name="rocom_merchant_subscription",
    display_name="远行商人订阅状态",
    priority=5,
    block=True,
    level="member",
    scene="all",
)


def _event_context(event: Event) -> tuple[str, str]:
    """根据事件判断订阅类型和标识键。

    返回 (sub_type, sub_key):
        - 群聊: ("group", str(group_id))
        - 私聊: ("private", user_id)
    """
    if event.is_group:
        return ("group", event.target.target_id)
    return ("private", event.user_id)


def _target_record(event: Event) -> dict[str, Any]:
    """保留旧 target 字段，并记录可精确定位连接的平台和 Bot。"""
    target: dict[str, Any] = {
        "user_id": event.user_id,
        "session_id": event.target.key(),
        "platform": event.platform,
        "bot_id": event.bot_id,
    }
    if event.is_group:
        target["group_id"] = event.target.target_id
    return target


@merchant_query.handle()
async def _handle_query(context: HandlerContext) -> None:
    """查询当前远行商人信息。"""
    try:
        snapshot = await fetch_merchant_snapshot()
        image = await render_merchant_image(snapshot)
    except Exception as exc:
        await context.finish(f"远行商人信息获取失败：{exc}")
    await context.finish(Message([MessageSegment.image(image)]))


@merchant_subscribe.handle()
async def _handle_subscribe(context: HandlerContext) -> None:
    """订阅远行商人推送（群聊或私聊）。"""
    event = context.event
    sub_type, sub_key = _event_context(event)
    if not sub_key:
        await context.finish("无法识别当前会话，订阅失败")
    state_store.upsert_subscription(sub_type, sub_key, _target_record(event), event.user_id)
    location = "本群" if sub_type == "group" else "私聊"
    await context.finish(f"已开启远行商人推送（{location}），将在远行商人数据更新时推送")


@merchant_unsubscribe.handle()
async def _handle_unsubscribe(context: HandlerContext) -> None:
    """取消远行商人推送（群聊或私聊）。"""
    sub_type, sub_key = _event_context(context.event)
    if not sub_key:
        await context.finish("无法识别当前会话，取消订阅失败")
    removed = state_store.remove_subscription(sub_type, sub_key)
    location = "本群" if sub_type == "group" else "私聊"
    if removed:
        await context.finish(f"已取消远行商人推送（{location}）")
    else:
        await context.finish(f"当前{location}没有远行商人推送订阅")


@merchant_status.handle()
async def _handle_status(context: HandlerContext) -> None:
    """查看远行商人推送订阅状态（群聊或私聊）。"""
    sub_type, sub_key = _event_context(context.event)
    if not sub_key:
        await context.finish("无法识别当前会话")
    sub = state_store.get_subscription(sub_type, sub_key)
    location = "本群" if sub_type == "group" else "该私聊"
    if not sub:
        await context.finish(f"{location}没有远行商人推送订阅")
    await context.finish(f"{location}已开启远行商人推送，远行商人数据更新时会推送")
