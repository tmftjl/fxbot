"""Bot 监控：掉线与假在线共用一套判据，连续失败后重启对应容器。"""

from __future__ import annotations

from .monitor import INTERVAL_SECONDS, run_tick
from ...core.plugin import PluginMetadata, scheduled

__plugin_meta__ = PluginMetadata(
    name="Bot 监控",
    description="每 5 分钟同时判定掉线与假在线，连续 3 次失败后重启账号对应的容器并私聊提醒超级用户。",
    usage="无需配置；检测间隔、失败阈值与重启节流均为固定值。",
)


@scheduled(interval=INTERVAL_SECONDS, run_on_start=True, name="bots")
async def watch_bots(context) -> None:
    await run_tick(context.app_services, context.state)
