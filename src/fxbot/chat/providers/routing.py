"""按任务和会话选择单一 Provider，并记录任务用量。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from time import perf_counter
from typing import Any
from contextlib import AsyncExitStack
from dataclasses import replace
from collections.abc import AsyncGenerator

from .usage import track_model_task_usage
from .manager import provider_manager
from ...config import get_manager as get_config_manager
from .entities import LLMRequest, TokenUsage, LLMResponse, JSONResponse, LLMResponseError
from ..bindings import PROVIDER, get_binding
from ..model_trace import record_model_call


def _call_argument(args: tuple[Any, ...], kwargs: dict[str, Any], name: str, index: int) -> Any:
    """优先取关键字参数，其次按 Provider 方法签名位置回退。"""
    if name in kwargs:
        return kwargs[name]
    return args[index] if len(args) > index else None


def candidate_providers(task: str, session_id: str = "") -> list[str]:
    """会话绑定优先，其次任务分配，最后使用默认 Provider。"""
    chat = get_config_manager().get_system()["chat"]
    routing = chat.get("model_routing") or {}
    available = set(provider_manager.list_providers_by_type("chat"))
    for primary in (get_binding(PROVIDER, session_id), str(routing.get(task) or "")):
        if primary in available:
            return [primary]
    return [""]


def routed_task(task: str, fallback: str = "") -> str:
    """任务未单独配置时沿用调用方指定的任务。"""
    if not fallback:
        return task
    try:
        routing = get_config_manager().get_system()["chat"].get("model_routing") or {}
        configured = str(routing.get(task) or "").strip() if isinstance(routing, dict) else ""
    except Exception:
        return fallback
    return task if configured else fallback


class TaskProvider:
    """维护单一任务模型的租约与显式用量汇总，不执行故障转移。"""

    def __init__(self, candidates: list[str], *, task: str = "") -> None:
        self._name = (candidates or [""])[0]
        self._stack = AsyncExitStack()
        self._provider: Any = None
        self.task = task
        self.usage = TokenUsage()

    async def __aenter__(self) -> TaskProvider:
        self._provider = await self._stack.enter_async_context(provider_manager.lease(self._name))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    @property
    def name(self) -> str:
        """返回实际 Provider 名称。"""
        return str(getattr(self._provider, "provider_id", "") or self._name)

    def __getattr__(self, item: str) -> Any:
        provider = self.__dict__.get("_provider")
        if provider is None:
            raise AttributeError(item)
        return getattr(provider, item)

    async def _prepare_request(self, context: Any, request: LLMRequest, hooks_enabled: bool) -> None:
        from ..hooks import dispatch

        if context is not None:
            await context.ensure_current()
        if context is not None and hooks_enabled and request.use_hooks:
            await dispatch("llm_request", context, request)
        if request.tool_registry is not None:
            if context is None:
                raise RuntimeError("动态工具需要聊天上下文")
            allowed, definitions = await context.tools(request.tool_registry)
            if request.tools is None:
                request.tools = definitions
            else:
                request.tools = [
                    item
                    for item in request.tools
                    if item["function"]["name"] in allowed
                    or request.tool_registry.get(item["function"]["name"]) is None
                ]
        if context is not None:
            await context.ensure_current()

    def _request_snapshot(self, request: LLMRequest | None, kwargs: dict[str, Any]) -> LLMRequest:
        """记录实际执行的模型与消息，后续续写不猜测原始路由或重复注入系统段。"""
        request = request or LLMRequest(phase=self.task)
        return replace(
            request,
            messages=deepcopy(kwargs.get("messages") or []),
            provider_id=self.name,
            model=kwargs.get("model") or self._provider.get_model(),
            temperature=kwargs.get("temperature", 0.7),
            max_tokens=kwargs.get("max_tokens"),
            tools=deepcopy(kwargs.get("tools")),
            system_prompt="",
            image_urls=[],
        )

    def _call_request(self, args: tuple, kwargs: dict, explicit: LLMRequest | None) -> LLMRequest | None:
        if explicit is not None:
            return explicit
        if self.task not in {"reply", "planner"}:
            return None
        return LLMRequest(
            messages=deepcopy(_call_argument(args, kwargs, "messages", 0) or []),
            model=_call_argument(args, kwargs, "model", 1),
            temperature=kwargs.get("temperature", args[2] if len(args) > 2 else 0.7),
            tools=deepcopy(_call_argument(args, kwargs, "tools", 3)),
            max_tokens=_call_argument(args, kwargs, "max_tokens", 4),
            phase=self.task,
        )

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        """使用选定 Provider 生成回复，并落一份请求快照。"""
        from ..hooks import dispatch
        from ..context import current_chat

        explicit = kwargs.pop("_request", None)
        hooks_enabled = kwargs.pop("_extension_hooks", True)
        merge_into = str(kwargs.pop("_trace_merge", "") or "")
        workflow_purpose = str(kwargs.pop("_trace_purpose", "") or "")
        workflow_attempt = int(kwargs.pop("_trace_attempt", 1) or 1)
        context = current_chat.get()
        request = self._call_request(args, kwargs, explicit)
        if request is not None:
            await self._prepare_request(context, request, hooks_enabled)
            args, kwargs = (), request.chat_kwargs()
            if request.provider_id is not None and request.provider_id not in {self.name, self._name}:
                async with TaskProvider([request.provider_id], task=self.task) as selected:
                    response = await selected.chat(
                        _request=request,
                        _extension_hooks=False,
                        _trace_merge=merge_into,
                        _trace_purpose=workflow_purpose,
                        _trace_attempt=workflow_attempt,
                    )
                    self.usage.merge(response.usage)
                if context is not None and hooks_enabled and request.use_hooks:
                    await dispatch("llm_response", context, request, response)
                if context is not None:
                    await context.ensure_current()
                return response
        started = perf_counter()
        response: LLMResponse | None = None
        error = ""
        try:
            async with track_model_task_usage(self.task, self._provider):
                response = await asyncio.wait_for(
                    self._provider.chat(*args, **kwargs), request.timeout if request is not None else None
                )
            response.provider_data = {**(response.provider_data or {}), "_provider_id": self.name}
            actual = (
                kwargs
                if request is not None
                else {
                    "messages": _call_argument(args, kwargs, "messages", 0),
                    "model": _call_argument(args, kwargs, "model", 1),
                    "temperature": kwargs.get("temperature", args[2] if len(args) > 2 else 0.7),
                    "tools": _call_argument(args, kwargs, "tools", 3),
                    "max_tokens": _call_argument(args, kwargs, "max_tokens", 4),
                }
            )
            response.request = self._request_snapshot(request, actual)
            if context is not None and request is not None and hooks_enabled and request.use_hooks:
                await dispatch("llm_response", context, request, response)
            if context is not None and request is not None:
                await context.ensure_current()
            return response
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, LLMResponseError):
                response = exc.response
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if response is not None:
                self.usage.merge(response.usage)
            key = self._record(
                messages=_call_argument(args, kwargs, "messages", 0),
                tools=_call_argument(args, kwargs, "tools", 3),
                response=response,
                duration_ms=(perf_counter() - started) * 1000,
                error=error,
                workflow_purpose=workflow_purpose,
                workflow_attempt=workflow_attempt,
                merge_into=merge_into,
                previous_purpose="replyer.reasoning_initial" if merge_into else "",
                model_name=request.model if request is not None else None,
            )
            if key and response is not None:
                response._trace_key = key

    async def chat_continuation(self, *args: Any, **kwargs: Any) -> LLMResponse | None:
        """续写也经过模型 hook；切换模型或 Provider 时不重放不兼容的原生状态。"""
        from ..hooks import dispatch
        from ..context import current_chat

        previous = _call_argument(args, kwargs, "previous", 1)
        original = previous.request
        context = current_chat.get()
        hooks_enabled = kwargs.pop("_extension_hooks", True)
        request = kwargs.pop("_request", None)
        if request is None:
            request = replace(
                original or LLMRequest(),
                messages=deepcopy(_call_argument(args, kwargs, "messages", 0) or []),
                model=_call_argument(args, kwargs, "model", 2) or (original.model if original else None),
                temperature=kwargs.get(
                    "temperature", args[3] if len(args) > 3 else (original.temperature if original else 0.7)
                ),
                max_tokens=kwargs.get(
                    "max_tokens", args[4] if len(args) > 4 else (original.max_tokens if original else None)
                ),
                provider_id=original.provider_id if original else (previous.provider_data or {}).get("_provider_id"),
                timeout=original.timeout if original else 240,
                use_hooks=original.use_hooks if original else True,
                phase=self.task,
                operation="continuation",
            )
        apply_hooks = context is not None and self.task in {"reply", "planner"}
        await self._prepare_request(context if apply_hooks else None, request, hooks_enabled)
        source_provider = (original.provider_id if original else None) or self.name
        source_model = original.model if original else self._provider.get_model()
        target_provider = request.provider_id if request.provider_id is not None else source_provider
        if target_provider not in {self.name, self._name}:
            async with TaskProvider([target_provider], task=self.task) as selected:
                response = await selected.chat_continuation(previous=previous, _request=request, _extension_hooks=False)
                if response is not None:
                    self.usage.merge(response.usage)
            if response is not None and apply_hooks and hooks_enabled and request.use_hooks:
                await dispatch("llm_response", context, request, response)
            if response is not None and context is not None:
                await context.ensure_current()
            return response
        target_model = request.model or self._provider.get_model()
        changed = self.name != source_provider or target_model != source_model or bool(request.tools)
        if changed:
            # 新目标不能使用旧目标的原生 response id / reasoning items，只发送可移植消息。
            request.messages = [
                {key: value for key, value in item.items() if key != "provider_data"}
                for item in request.chat_kwargs()["messages"]
            ]
            request.system_prompt, request.image_urls = "", []
            options = {
                "_request": request,
                "_extension_hooks": False,
            }
            trace_key = str(getattr(previous, "_trace_key", "") or "")
            if trace_key:
                options.update(
                    _trace_merge=trace_key,
                    _trace_purpose="replyer.reasoning_continuation",
                    _trace_attempt=0,
                )
            response = await self.chat(**options)
            if response is not None and apply_hooks and hooks_enabled and request.use_hooks:
                await dispatch("llm_response", context, request, response)
            if response is not None and context is not None:
                await context.ensure_current()
            return response
        call = request.chat_kwargs()
        call.pop("tools")
        call["previous"] = previous
        started = perf_counter()
        response: LLMResponse | None = None
        error = ""
        try:
            async with track_model_task_usage(self.task, self._provider):
                response = await asyncio.wait_for(self._provider.chat_continuation(**call), request.timeout)
            if response is not None:
                response.provider_data = {**(response.provider_data or {}), "_provider_id": self.name}
                response.request = self._request_snapshot(request, call)
                if apply_hooks and hooks_enabled and request.use_hooks:
                    await dispatch("llm_response", context, request, response)
                if context is not None:
                    await context.ensure_current()
            return response
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, LLMResponseError):
                response = exc.response
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if response is not None:
                self.usage.merge(response.usage)
            if response is not None or error:
                replay = (getattr(previous, "provider_data", None) or {}).get("output_items") or []
                merge_into = str(getattr(previous, "_trace_key", "") or "")
                key = self._record(
                    messages=[*call["messages"], *replay],
                    tools=None,
                    response=response,
                    duration_ms=(perf_counter() - started) * 1000,
                    error=error,
                    workflow_purpose="replyer.reasoning_continuation" if merge_into else "",
                    workflow_attempt=0,
                    merge_into=merge_into,
                    previous_purpose="replyer.reasoning_initial" if merge_into else "",
                    model_name=request.model,
                )
                if key and response is not None:
                    response._trace_key = key

    async def chat_json(self, *args: Any, **kwargs: Any) -> JSONResponse:
        """使用选定 Provider 生成结构化内容。"""
        started = perf_counter()
        response: LLMResponse | None = None
        error = ""
        try:
            async with track_model_task_usage(self.task, self._provider):
                result = await self._provider.chat_json(*args, **kwargs)
                response = result.response
            return result
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, LLMResponseError):
                response = exc.response
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if response is not None:
                self.usage.merge(response.usage)
            self._record(
                messages=_call_argument(args, kwargs, "messages", 0),
                tools=None,
                response=response,
                duration_ms=(perf_counter() - started) * 1000,
                error=error,
            )

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[LLMResponse, None]:
        """与普通调用共享请求 hook；完整消费后派发一次响应 hook，取消仍关闭原生流。"""
        from ..hooks import dispatch
        from ..context import current_chat

        context = current_chat.get()
        explicit = kwargs.pop("_request", None)
        hooks_enabled = kwargs.pop("_extension_hooks", True)
        request = self._call_request(args, kwargs, explicit)
        if request is not None:
            await self._prepare_request(context, request, hooks_enabled)
            args, kwargs = (), request.chat_kwargs()
            if request.provider_id is not None and request.provider_id not in {self.name, self._name}:
                async with TaskProvider([request.provider_id], task=self.task) as selected:
                    stream = selected.chat_stream(_request=request, _extension_hooks=False)
                    try:
                        async for response in stream:
                            if (
                                not response.is_stream_chunk
                                and context is not None
                                and request.use_hooks
                                and hooks_enabled
                            ):
                                await dispatch("llm_response", context, request, response)
                            if context is not None:
                                await context.ensure_current()
                            yield response
                    finally:
                        await stream.aclose()
                        self.usage.merge(selected.usage)
                return
        started = perf_counter()
        chunks: list[str] = []
        reasoning: list[str] = []
        final: LLMResponse | None = None
        error = ""
        completed = False
        timeout = request.timeout if request is not None else None
        deadline = asyncio.get_running_loop().time() + timeout if timeout is not None else None
        try:
            async with track_model_task_usage(self.task, self._provider):
                stream = self._provider.chat_stream(*args, **kwargs)
                try:
                    while True:
                        if context is not None and request is not None:
                            await context.ensure_current()
                        remaining = (
                            max(0, deadline - asyncio.get_running_loop().time()) if deadline is not None else None
                        )
                        try:
                            response = await asyncio.wait_for(anext(stream), remaining)
                        except StopAsyncIteration:
                            break
                        if response.is_stream_chunk:
                            chunks.append(str(response.content or ""))
                            reasoning.append(response.reasoning_content or "")
                            if context is not None and request is not None:
                                await context.ensure_current()
                            yield response
                        else:
                            final = response
                            break
                finally:
                    await stream.aclose()
            final = final or LLMResponse(content="".join(chunks), reasoning_content="".join(reasoning))
            actual = (
                kwargs
                if request is not None
                else {
                    "messages": _call_argument(args, kwargs, "messages", 0),
                    "model": _call_argument(args, kwargs, "model", 1),
                    "temperature": kwargs.get("temperature", args[2] if len(args) > 2 else 0.7),
                    "tools": _call_argument(args, kwargs, "tools", 3),
                    "max_tokens": _call_argument(args, kwargs, "max_tokens", 4),
                }
            )
            final.request = self._request_snapshot(request, actual)
            final.provider_data = {**(final.provider_data or {}), "_provider_id": self.name}
            if context is not None and request is not None:
                await context.ensure_current()
                if hooks_enabled and request.use_hooks:
                    await dispatch("llm_response", context, request, final)
                await context.ensure_current()
            completed = True
            yield final
        except (Exception, asyncio.CancelledError, GeneratorExit) as exc:
            if isinstance(exc, LLMResponseError):
                final = exc.response
            if not completed:
                error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            response = final or LLMResponse(content="".join(chunks))
            self.usage.merge(response.usage)
            key = self._record(
                messages=_call_argument(args, kwargs, "messages", 0),
                tools=_call_argument(args, kwargs, "tools", 3),
                response=response,
                duration_ms=(perf_counter() - started) * 1000,
                error=error,
                model_name=request.model if request is not None else None,
            )
            if key:
                response._trace_key = key

    def _record(
        self,
        *,
        messages: Any,
        tools: Any,
        response: LLMResponse | None,
        duration_ms: float,
        error: str,
        workflow_purpose: str = "",
        workflow_attempt: int = 1,
        merge_into: str = "",
        previous_purpose: str = "",
        model_name: str | None = None,
    ) -> str:
        """写入请求快照；失败不影响模型调用。"""
        provider_name = str(getattr(self._provider, "provider_id", "") or self._name)
        try:
            model = str(self._provider.get_model() or "")
        except Exception:
            model = ""
        model = model_name or model
        return record_model_call(
            task=self.task,
            provider=provider_name,
            model=model,
            messages=messages,
            tools=tools,
            response=response,
            duration_ms=int(duration_ms),
            error=error,
            workflow_purpose=workflow_purpose,
            workflow_attempt=workflow_attempt,
            merge_into=merge_into,
            previous_purpose=previous_purpose,
        )


def fallback_provider(task: str, session_id: str = "") -> TaskProvider:
    """按会话绑定、任务分配构造单一模型上下文。"""
    return TaskProvider(candidate_providers(task, session_id), task=task)
