"""娱乐插件元信息。"""

from . import (
    box as box,
    doro as doro,
    sick as sick,
    tools as tools,
    napcat as napcat,
    fortune as fortune,
    welcome as welcome,
    musicshare as musicshare,
    image_display as image_display,
)
from ...core.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="娱乐", description="运势、点赞、资料卡、欢迎语、点歌与趣味语录。", usage="#今日运势；#赞我；#点歌 歌曲名"
)
