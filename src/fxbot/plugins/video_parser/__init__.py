"""视频解析插件元信息。"""

from . import matcher as matcher
from ...core.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="视频解析",
    description="解析受支持平台的媒体链接并发送结果。",
    usage="发送视频链接；#开启解析；#关闭解析",
)
