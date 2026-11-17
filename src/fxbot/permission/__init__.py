"""核心权限系统导出。"""

from .types import PermLevel, PermScene, PermContext, perm_entry_default
from .policy import FeaturePolicy
from .helpers import upsert_plugin_defaults, upsert_command_defaults
from .storage import PermissionStorage, get_storage
from .identity import superuser_entries, is_superuser_event, is_superuser_identity

__all__ = [
    "PermContext",
    "PermLevel",
    "PermScene",
    "PermissionStorage",
    "FeaturePolicy",
    "get_storage",
    "is_superuser_event",
    "is_superuser_identity",
    "perm_entry_default",
    "superuser_entries",
    "upsert_command_defaults",
    "upsert_plugin_defaults",
]
