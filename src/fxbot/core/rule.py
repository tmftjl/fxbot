"""响应规则组合：全部条件通过才响应，不隐式转换返回值。"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from .router import HandlerContext

Checker = Callable[[HandlerContext], Awaitable[bool]]


@dataclass(frozen=True, slots=True, init=False)
class Rule:
    checkers: tuple[Checker, ...]

    def __init__(self, *checkers: Checker) -> None:
        object.__setattr__(self, "checkers", checkers)

    def __and__(self, other: Rule | Checker) -> Rule:
        return Rule(*self.checkers, *(other.checkers if isinstance(other, Rule) else (other,)))

    async def __call__(self, context: HandlerContext) -> bool:
        for checker in self.checkers:
            if await checker(context) is not True:
                return False
        return True
