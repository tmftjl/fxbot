"""实用工具插件元信息。"""

from . import dps_chart as dps_chart, cos_upload as cos_upload, waves_analyze as waves_analyze
from ...core import HandlerContext
from .ui_schema import get_ui_schema
from ...core.plugin import PluginMetadata, on_regex

__plugin_extensions__ = {"console.config": get_ui_schema}

__plugin_meta__ = PluginMetadata(
    name="实用工具",
    description="鸣潮 DPS 榜、面板评分与 COS 图片收集。",
    usage="wwdps；ww更新dps；ww评分；#上传cos；#cos列表",
)

panel_refresh_cmd = on_regex(
    r"^ww(?:刷新|更新)?面板(?:刷新)?",
    name="refresh",
    display_name="刷新面板提示",
    priority=5,
    block=True,
    level="all",
    scene="all",
)


@panel_refresh_cmd.handle()
async def _handle_panel_refresh(context: HandlerContext) -> None:
    """发送面板刷新提示。"""
    await context.finish("更新已完成，下次刷单个角色请使用 `ww刷新【角色名】面板`")
