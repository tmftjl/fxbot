"""会话调度所有权和应用托管任务，不内置插件业务心流。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

if TYPE_CHECKING:
    from .context import ChatContext


@dataclass
class GateDecision:
    """None 沿用默认判断；force=True 跳过概率与冷却，不能绕过基础权限。"""

    allow: bool = True
    force: bool | None = None
    debounce: bool = True


@dataclass
class ScheduleOwner:
    name: str
    receive: Callable[[ChatContext], Awaitable[None]]
    context: ChatContext
    allow_rest_time: bool = True
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class ChatScheduling:
    def __init__(self) -> None:
        self.owners: dict[str, ScheduleOwner] = {}
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._task_owners: dict[tuple[str, str], ScheduleOwner] = {}
        self._task_contexts: dict[tuple[str, str], ChatContext] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._transitions: dict[str, asyncio.Future[None]] = {}
        self._settling: set[asyncio.Task] = set()
        self._transition_tasks: dict[asyncio.Future, asyncio.Task] = {}
        self._stopping = False

    def start(self) -> None:
        if self.owners or self.tasks or self._transitions or self._transition_tasks or self._settling:
            raise RuntimeError("调度服务仍有未完成的所有权或交接")
        self._stopping = False

    def _begin_transition(self, session_id: str) -> tuple[asyncio.Future | None, asyncio.Future]:
        if self._stopping:
            raise RuntimeError("调度服务正在关闭")
        if asyncio.current_task() in self._settling:
            raise RuntimeError("调度交接取消回调不能再次发起交接")
        previous = self._transitions.get(session_id)
        current = asyncio.get_running_loop().create_future()
        self._transitions[session_id] = current
        self._transition_tasks[current] = asyncio.current_task()
        return previous, current

    def _finish_transition(self, session_id: str, transition: asyncio.Future) -> None:
        self._transition_tasks.pop(transition, None)
        if not transition.done():
            transition.set_result(None)
        if self._transitions.get(session_id) is transition:
            self._transitions.pop(session_id, None)

    async def _settle(self, tasks: list[asyncio.Task]) -> None:
        pending = [task for task in tasks if task is not asyncio.current_task()]
        self._settling.update(pending)
        try:
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            if pending:
                joined = asyncio.gather(*pending, return_exceptions=True)
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError:
                    await joined
                    raise
        finally:
            self._settling.difference_update(pending)

    def owner(self, session_id: str) -> ScheduleOwner | None:
        return self.owners.get(session_id)

    async def claim(
        self,
        name: str,
        context: ChatContext,
        receive: Callable[[ChatContext], Awaitable[None]],
        *,
        replace: bool = False,
        allow_rest_time: bool = True,
    ) -> None:
        """切换前停止默认待处理与活动执行；所有权在清理期间也阻止默认启动。"""
        from .focus import focus_mode_manager
        from .behavior.turn_scheduler import normal_scheduler

        if self._stopping:
            raise RuntimeError("调度服务正在关闭")
        session_id = context.session_id
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            if self._stopping:
                raise RuntimeError("调度服务已停止")
            existing = self.owners.get(session_id)
            if existing is not None and existing.name != name and not replace:
                raise ValueError(f"会话已由 {existing.name} 调度")
            if existing is not None and existing.name == name:
                existing.receive, existing.context, existing.allow_rest_time = receive, context, allow_rest_time
                context.schedule_owner = existing
                return
            previous, transition = self._begin_transition(session_id)
            selected = ScheduleOwner(name, receive, context, allow_rest_time)
            self.owners[session_id] = selected
            context.schedule_owner = selected
        try:
            if previous is not None:
                await asyncio.shield(previous)
            if self.owners.get(session_id) is not selected:
                return
            state = normal_scheduler.chats.get(session_id)
            if state is not None and state.task is not None:
                self._settling.add(state.task)
            try:
                normal_scheduler.clear(session_id, exclude_current=True)
                if state is not None and state.task is not None and state.task is not asyncio.current_task():
                    await asyncio.gather(state.task, return_exceptions=True)
            finally:
                if state is not None and state.task is not None:
                    self._settling.discard(state.task)
            from .continuation import reply_scheduler

            reply_scheduler.cancel(session_id=session_id)
            pending = [
                task
                for current, task in tuple(context.service._running.values())
                if current.session_id == session_id and task is not asyncio.current_task()
            ]
            await self._settle(pending)
            await focus_mode_manager.detach(session_id)
            if existing is not None:
                await self._cancel_tasks(session_id, owner=existing)
        except BaseException:
            if self.owners.get(session_id) is selected:
                self.owners.pop(session_id, None)
            raise
        finally:
            selected.ready.set()
            self._finish_transition(session_id, transition)

    async def release(self, name: str, session_id: str) -> None:
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            current = self.owners.get(session_id)
            if current is None or current.name != name:
                return
            previous, transition = self._begin_transition(session_id)
            current.ready.clear()
        try:
            if previous is not None:
                await asyncio.shield(previous)
            if self.owners.get(session_id) is not current:
                return
            await self._cancel_tasks(session_id, owner=current)
            service = current.context.service
            pending = [
                task
                for context, task in tuple(service._running.values())
                if context.session_id == session_id and task is not asyncio.current_task()
            ]
            await self._settle(pending)
        finally:
            if self.owners.get(session_id) is current:
                self.owners.pop(session_id, None)
            current.ready.set()
            self._finish_transition(session_id, transition)

    async def deliver(self, context: ChatContext) -> bool:
        """等待所有权交接完成再投递消息，不在交接清理时启动新轮次。"""
        while True:
            if self._stopping:
                return False
            owner = self.owners.get(context.session_id)
            if owner is None:
                return False
            await owner.ready.wait()
            if self.owners.get(context.session_id) is owner:
                break
        context.schedule_owner = owner
        await owner.receive(context)
        return True

    def release_owner(self, name: str) -> None:
        """同步撤销身份并取消任务；应用仍拥有并等待任务的 finally。"""
        for session_id, owner in tuple(self.owners.items()):
            if owner.name == name:
                self.owners.pop(session_id, None)
                owner.ready.set()
                owner.context.service.cancel(session_id, exclude_current=True)
                self.cancel(session_id, exclude_current=True)

    def cancel(self, session_id: str, *, exclude_current: bool = False) -> None:
        for (tenant, _), task in tuple(self.tasks.items()):
            if tenant == session_id and not (exclude_current and task is asyncio.current_task()):
                if not task.cancelling():
                    task.cancel()

    async def _cancel_tasks(self, session_id: str, *, owner: ScheduleOwner | None = None) -> None:
        pending = [
            task
            for (tenant, key), task in tuple(self.tasks.items())
            if tenant == session_id
            and task is not asyncio.current_task()
            and (owner is None or self._task_owners.get((tenant, key)) is owner)
        ]
        await self._settle(pending)

    async def cancel_context(self, context: ChatContext) -> None:
        """断开的临时通道只取消从该发送通道派生的任务，不影响下一次连接。"""
        pending = [
            task
            for key, task in tuple(self.tasks.items())
            if self._task_contexts[key].runtime.sender is context.runtime.sender
        ]
        await self._settle(pending)

    def spawn(
        self,
        context: ChatContext,
        operation: Callable[[], Awaitable[Any]],
        *,
        key: str = "main",
        delay: float = 0,
    ) -> asyncio.Task:
        """定时唤醒或持续心流；相同 key 不隐式重启副作用操作。"""
        if self._stopping:
            raise RuntimeError("调度服务正在关闭")
        owner = self.owners.get(context.session_id)
        if self._stopping:
            raise RuntimeError("调度服务已停止")
        if owner is None:
            raise RuntimeError("请先声明会话调度所有权")
        if context.schedule_owner is not owner:
            raise RuntimeError("此上下文不属于当前调度所有者")
        application = context.runtime.application
        if application is None:
            raise RuntimeError("后台心流需要实际应用拥有任务")
        slot = (context.session_id, key)
        if slot in self.tasks:
            raise ValueError("调度任务 key 已存在，请先取消旧任务")
        epoch = context.sessions.epoch(context.session_id)

        async def run() -> Any:
            try:
                await owner.ready.wait()
                if delay:
                    await asyncio.sleep(delay)
                if self.owners.get(context.session_id) is not owner:
                    return None
                if not context.service.is_current(context.session_id, epoch):
                    return None
                return await operation()
            finally:
                if self.tasks.get(slot) is asyncio.current_task():
                    self.tasks.pop(slot, None)
                    self._task_owners.pop(slot, None)
                    self._task_contexts.pop(slot, None)

        task = application.spawn(run(), name=f"chat-flow:{owner.name}:{key}")
        self.tasks[slot] = task
        self._task_owners[slot] = owner
        self._task_contexts[slot] = context
        return task

    async def stop(self) -> None:
        self._stopping = True
        pending = list({*self.tasks.values(), *self._transition_tasks.values()})
        for owner in self.owners.values():
            owner.ready.set()
            pending.extend(
                task
                for context, task in tuple(owner.context.service._running.values())
                if context.session_id == owner.context.session_id
            )
        self.owners.clear()
        await self._settle(list(set(pending)))
        self.tasks.clear()
        self._task_owners.clear()
        self._task_contexts.clear()
        self._locks.clear()
        self._transitions.clear()
        self._transition_tasks.clear()


chat_scheduling = ChatScheduling()
