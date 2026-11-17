"""远行商人后台推送任务。"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from .store import RocomStore, store as state_store, subscription_target
from ...core import Message, MessageSegment
from .client import MerchantSnapshot, fetch_merchant_snapshot
from .config import get_config
from .renderer import render_merchant_image
from ...utils.tz import SHANGHAI_TZ
from ...core.plugin import scheduled
from ...core.scheduler import Daily

MERCHANT_RETRY_TIMES = 20
MERCHANT_RETRY_INTERVAL_SECONDS = 30


async def _send_to_subscription(subscription: dict[str, Any], image: bytes, notifications, store: RocomStore) -> bool:
    target = subscription_target(subscription)
    if target is None:
        logger.warning("[rocom] 跳过缺少平台或 Bot 身份的订阅: {}", subscription.get("type"))
        return False
    sub_type = str(subscription.get("type"))
    key_field = "group_key" if sub_type == "group" else "user_key"
    sub_key = str(subscription.get(key_field) or "")

    def valid() -> bool:
        return store.get_subscription(sub_type, sub_key) is not None

    try:
        return await notifications.send(
            "rocom",
            target,
            Message([MessageSegment.image(image)]),
            valid=valid,
        )
    except Exception:
        logger.opt(exception=True).warning("[rocom] 远行商人推送到 {} 失败", target.key())
        return False


async def push_snapshot(snapshot: MerchantSnapshot, notifications, store: RocomStore) -> int:
    """向所有订阅目标推送快照图片。"""
    subscriptions = store.get_subscriptions()
    if not subscriptions:
        return 0
    image = await render_merchant_image(snapshot)
    group_count = sum(1 for item in subscriptions if item.get("type", "group") == "group")
    private_count = sum(1 for item in subscriptions if item.get("type") == "private")
    logger.info(f"[rocom] 开始推送远行商人刷新: {group_count} 个群订阅, {private_count} 个私聊订阅")
    pushed = 0
    for subscription in subscriptions:
        if await _send_to_subscription(subscription, image, notifications, store):
            pushed += 1
    logger.info(f"[rocom] 远行商人推送完成: {pushed}/{len(subscriptions)} 个目标")
    return pushed


async def calibrate_current_signature(
    store: RocomStore | None = None,
    fetcher=None,
) -> None:
    """启动时校准当前快照签名，不触发推送。"""
    state = state_store if store is None else store
    fetch = fetch_merchant_snapshot if fetcher is None else fetcher
    snapshot = await fetch()
    state.set_last_signature(snapshot.signature)


async def check_and_push(
    notifications,
    *,
    store: RocomStore | None = None,
    fetcher=None,
) -> bool:
    """检查远行商人变化并推送所有订阅。"""
    state = state_store if store is None else store
    fetch = fetch_merchant_snapshot if fetcher is None else fetcher
    snapshot = await fetch()
    last_signature = state.get_last_signature()
    if snapshot.signature == last_signature:
        return False

    if not snapshot.products:
        return False

    pushed = await push_snapshot(snapshot, notifications, state)
    state.set_last_signature(snapshot.signature)
    if pushed:
        logger.info(f"[rocom] 已推送远行商人刷新：{pushed} 个目标")
    return True


async def check_and_push_with_retry(notifications, *, store: RocomStore | None = None) -> None:
    """刷新点后短重试，直到拿到新商品并推送。"""
    config = get_config()["merchant"]
    retry_times = int(config.get("retry_times") or MERCHANT_RETRY_TIMES)
    retry_interval = int(config.get("retry_interval_seconds") or MERCHANT_RETRY_INTERVAL_SECONDS)
    for index in range(retry_times):
        if await check_and_push(notifications, store=store):
            return
        if index + 1 < retry_times:
            await asyncio.sleep(retry_interval)
    logger.info("[rocom] 远行商人刷新点检查结束：未发现新的可推送商品")


def merchant_plan(context) -> Daily | None:
    """远行商人轮次结束后 5 分钟检查，配置保存后由核心重新排期。"""
    config = get_config()["merchant"]
    if not config["enabled"]:
        return None
    return Daily(tuple(int(hour) for hour in config["push_hours"]), config["push_minute"], SHANGHAI_TZ)


@scheduled(plan=merchant_plan, run_on_start=True, watch_config=True, name="merchant")
async def merchant(context) -> None:
    try:
        # 没有订阅时不访问官方接口：启动校准也只在确实需要推送时才有意义。
        if not state_store.get_subscriptions():
            return
        if context.first_run:
            await calibrate_current_signature()
            return
        await check_and_push_with_retry(context.notifications)
    except Exception:
        logger.opt(exception=True).warning("[rocom] 远行商人推送检查失败")
