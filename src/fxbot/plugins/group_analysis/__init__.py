"""群聊日报插件。"""

from . import commands as commands, scheduler as scheduler
from .ui_schema import get_ui_schema
from ...core.plugin import PluginMetadata

__plugin_extensions__ = {"console.config": get_ui_schema}

__plugin_meta__ = PluginMetadata(name="群聊日报", description="统计群消息并生成日报。", usage="#群聊分析 [天数]")
