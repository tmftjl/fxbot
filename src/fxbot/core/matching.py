"""统一的匹配产物与构造函数，业务不再重新解析捕获组。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Callable, Iterable

from .types import Event


@dataclass(frozen=True, slots=True)
class MatchResult:
    arguments: str = ""
    groups: tuple[str | None, ...] = ()
    named_groups: tuple[tuple[str, str | None], ...] = ()
    command: str = ""

    def group(self, key: int | str, default: str = "") -> str:
        if isinstance(key, str):
            return dict(self.named_groups).get(key) or default
        if not 1 <= key <= len(self.groups):
            raise IndexError("捕获组编号从 1 开始且必须存在")
        return self.groups[key - 1] or default


Match = Callable[[Event], MatchResult | None]


def command_match(command: str, aliases: Iterable[str] = (), *, prefixes: tuple[str, ...] = ("#", "＃", "/")) -> Match:
    if isinstance(aliases, str):
        raise TypeError("aliases 必须是命令词集合")
    words = (command, *aliases)
    if any(not isinstance(word, str) or not word or any(ch.isspace() for ch in word) for word in words):
        raise ValueError("命令词必须非空且不含空白")
    expression = re.compile(
        r"^(?:"
        + "|".join(re.escape(prefix) for prefix in prefixes)
        + r")("
        + "|".join(re.escape(word) for word in sorted(set(words)))
        + r")(?:\s+(.*))?$"
    )

    def match(event: Event) -> MatchResult | None:
        if not event.is_message:
            return None
        result = expression.fullmatch(event.message.plain_text().strip())
        return MatchResult(result[2] or "", command=result[1]) if result else None

    return match


def regex_match(pattern: str, flags: int = 0, *, strip: bool = True) -> Match:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("正则表达式不能为空")
    expression = re.compile(pattern, flags)

    def match(event: Event) -> MatchResult | None:
        if not event.is_message:
            return None
        text = event.message.plain_text()
        result = expression.search(text.strip() if strip else text)
        return MatchResult(result[0], result.groups(), tuple(result.groupdict().items())) if result else None

    return match


def message_match(event: Event) -> MatchResult | None:
    return MatchResult(event.message.plain_text().strip()) if event.is_message else None


def event_match(event_type: str) -> Match:
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("事件类型不能为空")
    return lambda event: MatchResult() if event.event_type == event_type else None
