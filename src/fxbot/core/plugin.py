"""插件开发公开 API。"""

from .rule import Rule
from .tasks import scheduled
from .binding import State, CommandArguments
from .matcher import Matcher
from .plugins import PluginMetadata, outgoing
from .commands import on_event, on_regex, on_command, on_message
from .matching import MatchResult
from .services import Service
from .contracts import ApplicationServices
from .permission import Permission

__all__ = [
    "PluginMetadata",
    "on_command",
    "on_regex",
    "on_event",
    "on_message",
    "scheduled",
    "outgoing",
    "Service",
    "Matcher",
    "MatchResult",
    "CommandArguments",
    "ApplicationServices",
    "State",
    "Rule",
    "Permission",
]
