"""框架依赖的窄接口，不导入配置、权限或通知的具体实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol
from collections.abc import Callable, Awaitable

from .types import Event, Message, SessionTarget

if TYPE_CHECKING:
    from .commands import Command
    from .scheduler import Scheduler
    from .application import Application


class Configuration(Protocol):
    def get_system(self) -> dict[str, Any]: ...
    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]: ...


class Authorization(Protocol):
    @property
    def configuration(self) -> Configuration: ...
    def guard(self, feature: str) -> Callable[[Event], Awaitable[bool]]: ...
    async def check(self, feature: str, event: Event) -> bool: ...
    async def check_target(self, feature: str, target: SessionTarget) -> bool: ...
    def declare_commands(self, feature: str, commands: tuple[Command, ...]) -> None: ...


class Notifications(Protocol):
    async def allowed(self, feature: str, target: SessionTarget, *, valid: Callable[[], bool]) -> bool: ...
    async def send(
        self, feature: str, target: SessionTarget, message: Message, *, valid: Callable[[], bool]
    ) -> bool: ...


class ApplicationServices(Protocol):
    @property
    def configuration(self) -> Configuration: ...
    @property
    def application(self) -> Application: ...
    @property
    def scheduler(self) -> Scheduler: ...
    @property
    def authorizer(self) -> Authorization: ...
    @property
    def notifications(self) -> Notifications: ...
