"""Cultured 图库插件元信息。"""

from . import commands as commands, update_gallery as update_gallery
from .ui_schema import get_ui_schema
from ...core.plugin import PluginMetadata

__plugin_extensions__ = {"console.config": get_ui_schema}

__plugin_meta__ = PluginMetadata(
    name="图库",
    description="随机本地图库、API 图库与图库仓库更新。",
    usage="#表情包列表；#随机<名称>；#来张<名称>；#图库更新",
)
