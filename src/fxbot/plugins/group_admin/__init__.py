"""群管理、违禁词和入群验证。"""

from . import tools as tools, banwords as banwords, commands as commands, join_verify as join_verify
from ...core.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="群管理", description="群管理、违禁词及入群验证码验证。", usage="#禁言 @成员 10分钟；#开启进群验证"
)
