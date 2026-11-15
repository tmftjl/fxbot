"""视频解析插件元信息。"""

from . import matcher as matcher
from .ui_schema import get_ui_schema
from ...core.plugin import PluginMetadata

__plugin_extensions__ = {"console.config": get_ui_schema}

__plugin_meta__ = PluginMetadata(
    name="视频解析",
    description="解析受支持平台的媒体链接并发送结果。",
    usage="发送视频链接；#开启解析；#关闭解析",
)
