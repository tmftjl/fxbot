"""响应器创建入口，公开参数显式声明，共享匹配构造。"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

from .rule import Rule, Checker
from .matcher import Matcher, MatcherOptions
from .matching import event_match, regex_match, command_match, message_match
from .permission import Permission


@dataclass(frozen=True, slots=True)
class Command:
    """权限服务消费的最小节点投影。"""

    name: str
    level: str = "member"
    scene: str = "all"


def on_command(
    command: str,
    *,
    aliases: Iterable[str] = (),
    name: str = "",
    display_name: str = "",
    level: str = "member",
    scene: str = "all",
    priority: int = 5,
    block: bool = True,
    rule: Rule | Checker | None = None,
    permission: Permission | Checker | None = None,
) -> Matcher:
    return Matcher(
        command_match(command, aliases),
        MatcherOptions(
            name=name or command,
            display_name=display_name or command,
            level=level,
            scene=scene,
            priority=priority,
            block=block,
            rule=Rule() if rule is None else Rule(rule),
            permission=Permission() if permission is None else Permission(permission),
        ),
    )


def on_regex(
    pattern: str,
    *,
    flags: int = 0,
    name: str = "",
    display_name: str = "",
    level: str = "member",
    scene: str = "all",
    priority: int = 5,
    block: bool = True,
    rule: Rule | Checker | None = None,
    permission: Permission | Checker | None = None,
) -> Matcher:
    return Matcher(
        regex_match(pattern, flags),
        MatcherOptions(
            name=name,
            display_name=display_name,
            level=level,
            scene=scene,
            priority=priority,
            block=block,
            rule=Rule() if rule is None else Rule(rule),
            permission=Permission() if permission is None else Permission(permission),
        ),
    )


def on_event(
    event_type: str,
    *,
    name: str = "",
    display_name: str = "",
    level: str = "all",
    scene: str = "all",
    priority: int = 5,
    block: bool = False,
    rule: Rule | Checker | None = None,
    permission: Permission | Checker | None = None,
) -> Matcher:
    return Matcher(
        event_match(event_type),
        MatcherOptions(
            name=name,
            display_name=display_name,
            level=level,
            scene=scene,
            priority=priority,
            block=block,
            rule=Rule() if rule is None else Rule(rule),
            permission=Permission() if permission is None else Permission(permission),
        ),
    )


def on_message(
    *,
    name: str = "",
    display_name: str = "",
    level: str = "all",
    scene: str = "all",
    priority: int = 5,
    block: bool = True,
    rule: Rule | Checker | None = None,
    permission: Permission | Checker | None = None,
) -> Matcher:
    return Matcher(
        message_match,
        MatcherOptions(
            name=name,
            display_name=display_name,
            level=level,
            scene=scene,
            priority=priority,
            block=block,
            rule=Rule() if rule is None else Rule(rule),
            permission=Permission() if permission is None else Permission(permission),
        ),
    )
