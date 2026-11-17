"""无限二代提示词扩展元信息。"""

from . import integration as integration
from .ui_schema import get_ui_schema
from ...core.plugin import PluginMetadata

__plugin_extensions__ = {"console.config": get_ui_schema}

__plugin_meta__ = PluginMetadata(
    name="无限二代",
    description="通过系统提示词段和纯人格直聊开关扩展聊天行为。",
    usage="在控制台「无限二代」中开关提示词注入和纯人格直聊。",
)
