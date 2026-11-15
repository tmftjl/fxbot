"""响应器附加权限；不能替代应用统一声明的权限节点。"""

from __future__ import annotations

from dataclasses import dataclass

from .rule import Checker
from .router import Guard, HandlerContext


@dataclass(frozen=True, slots=True, init=False)
class Permission:
    checkers: tuple[Checker, ...]

    def __init__(self, *checkers: Checker) -> None:
        object.__setattr__(self, "checkers", checkers)

    def __or__(self, other: Permission | Checker) -> Permission:
        return Permission(*self.checkers, *(other.checkers if isinstance(other, Permission) else (other,)))

    async def __call__(self, context: HandlerContext) -> bool:
        if not self.checkers:
            return True
        for checker in self.checkers:
            if await checker(context) is True:
                return True
        return False

    @classmethod
    def for_event(cls, guard: Guard) -> Permission:
        """在应用装配处将领域事件权限接入响应上下文。"""

        async def check(context: HandlerContext) -> bool:
            return await guard(context.event)

        return cls(check)
