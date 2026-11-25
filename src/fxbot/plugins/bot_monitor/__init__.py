"""Bot 掉线监控：上线过的 Bot 掉线后私聊提醒超级用户。"""

from __future__ import annotations

from .config import get_config
from .monitor import run_tick
from .ui_schema import get_ui_schema
from ...core.plugin import PluginMetadata, scheduled
from ...core.scheduler import Interval

__plugin_extensions__ = {"console.config": get_ui_schema}

__plugin_meta__ = PluginMetadata(
    name="Bot 监控",
    description="检测上线过的 Bot 掉线，并私聊提醒超级用户；掉线提醒后恢复时再补一条。",
    usage="无需命令，在控制台「Bot 监控」配置页调整检测间隔与提醒策略。",
)


def monitor_plan(context):
    """配置关闭时不排期；间隔变化由 watch_config 重新计算。"""
    del context
    config = get_config()
    if not config["enabled"] or config["interval_seconds"] <= 0:
        return None
    return Interval(float(config["interval_seconds"]))


@scheduled(plan=monitor_plan, run_on_start=True, watch_config=True, name="offline")
async def watch_offline(context) -> None:
    await run_tick(context.app_services, context.state)
