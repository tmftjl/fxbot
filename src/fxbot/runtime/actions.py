"""带关联标识、容量限制和超时的动作请求代理。"""

from __future__ import annotations

import uuid
import asyncio
from typing import Any
from dataclasses import dataclass
from collections.abc import Callable, Awaitable


class BotOffline(ConnectionError):
    """传输已关闭，不能提交动作。"""


class ActionFailed(RuntimeError):
    """平台动作失败，不携带上游响应内容。"""


@dataclass(frozen=True, slots=True)
class _Request:
    """一项请求独占响应 Future 和执行任务。"""

    response: asyncio.Future
    execution: asyncio.Task


class ActionBroker:
    """拥有请求的完整生命周期，连接只负责转交协议响应。"""

    def __init__(self, send: Callable[[dict], Awaitable[None]], *, timeout: float, capacity: int) -> None:
        """在所属事件循环内建立请求表和传输写锁。"""
        self._send = send
        self._timeout = timeout
        self._capacity = capacity
        self._write_lock = asyncio.Lock()
        self._offline = asyncio.get_running_loop().create_future()
        self._requests: dict[str, _Request] = {}

    @property
    def pending_count(self) -> int:
        """返回当前待完成请求数。"""
        return len(self._requests)

    async def call(self, action: str, params: dict[str, Any]) -> Any:
        """执行一次动作，超时和断连均不自动重试。"""
        if self._offline.done():
            raise BotOffline("Bot 离线")
        if self.pending_count >= self._capacity:
            raise ActionFailed("待处理动作已满")
        echo = uuid.uuid4().hex
        response = asyncio.get_running_loop().create_future()

        async def execute() -> Any:
            """串行写入协议帧，写锁不覆盖等待响应的阶段。"""
            async with self._write_lock:
                await self._send({"action": action, "params": params, "echo": echo})
            return await response

        execution = asyncio.create_task(execute(), name=f"protocol.action:{action}")
        self._requests[echo] = _Request(response, execution)
        try:
            done, _ = await asyncio.wait(
                (execution, self._offline), timeout=self._timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if self._offline.done():
                raise BotOffline("Bot 离线")
            if execution not in done:
                raise asyncio.TimeoutError("动作请求超时")
            return execution.result()
        finally:
            self._requests.pop(echo)
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            if not response.done():
                response.cancel()
            elif not response.cancelled():
                response.exception()

    def receive(self, payload: dict[str, Any]) -> None:
        """关联有效请求，忽略已超时或不属于当前连接的响应。"""
        echo = payload.get("echo")
        request = self._requests.get(echo) if isinstance(echo, str) else None
        if request is None or request.response.done():
            return
        if payload.get("status") == "ok" and type(payload.get("retcode")) is int and payload["retcode"] == 0:
            request.response.set_result(payload.get("data"))
        else:
            request.response.set_exception(ActionFailed("平台动作失败"))

    async def aclose(self) -> None:
        """先拒绝新请求，再中止所有执行中的传输和响应等待。"""
        if not self._offline.done():
            self._offline.set_result(None)
        requests = tuple(self._requests.values())
        for request in requests:
            request.execution.cancel()
            request.response.cancel()
        await asyncio.gather(*(request.execution for request in requests), return_exceptions=True)
