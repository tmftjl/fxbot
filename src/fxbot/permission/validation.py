"""权限配置的明确输入边界，非法限制不能被默认值放宽。"""

from .types import PermLevel, PermScene


def _fields(value: object, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError(f"{label} 必须是只包含已声明字段的对象")
    return value


def _identities(values: object) -> None:
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("权限身份必须为非空字符串数组")
    for value in values:
        if value != value.strip():
            raise ValueError("权限身份不能包含首尾空白")
        if ":" in value:
            raise ValueError("权限身份必须是 QQ 号、群号或 OpenID")


def validate_entry(value: object) -> None:
    entry = _fields(value, {"enabled", "level", "scene", "whitelist", "blacklist", "bot_admins"}, "权限条目")
    if type(entry.get("enabled", True)) is not bool:
        raise ValueError("权限开关必须为布尔值")
    PermLevel.from_str(entry.get("level", "member"))
    PermScene.from_str(entry.get("scene", "all"))
    for name in ("whitelist", "blacklist"):
        scope = _fields(entry.get(name, {}), {"users", "groups"}, name)
        _identities(scope.get("users", []))
        _identities(scope.get("groups", []))
    _identities(entry.get("bot_admins", []))


def validate_permissions(value: object) -> None:
    document = _fields(value, {"top", "sub_plugins", "bot_admins"}, "权限配置")
    if "top" not in document:
        raise ValueError("权限配置缺少全局策略")
    validate_entry(document["top"])
    _identities(document.get("bot_admins", []))
    plugins = document.get("sub_plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("插件权限必须为对象")
    for name, value in plugins.items():
        if not isinstance(name, str) or not name or ":" in name:
            raise ValueError("插件权限名称必须为非空且不含冒号的字符串")
        plugin = _fields(value, {"top", "commands"}, "插件权限")
        if "top" not in plugin:
            raise ValueError("插件权限缺少 top")
        validate_entry(plugin["top"])
        commands = plugin.get("commands", {})
        if not isinstance(commands, dict):
            raise ValueError("命令权限必须为对象")
        for command, entry in commands.items():
            if not isinstance(command, str) or not command or ":" in command:
                raise ValueError("命令权限名称必须为非空且不含冒号的字符串")
            validate_entry(entry)
