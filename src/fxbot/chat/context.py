"""开放的聊天工作上下文；模型能力与托管历史、发送分开。"""

from __future__ import annotations

import json
import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from contextvars import ContextVar
from dataclasses import replace

from .hooks import guard, dispatch, hook_context
from .types import ChatRequest, ChatResponse, is_proactive, is_continuation
from .session import history_limit
from .providers import LLMRequest, LLMResponse, provider_manager
from .tools.runtime import ToolRuntime
from .tools.registry import ToolRegistry, default_registry

if TYPE_CHECKING:
    from collections.abc import Callable, Awaitable

    from .service import ChatService


current_chat: ContextVar[ChatContext | None] = ContextVar("fxbot_chat", default=None)


class ChatExpired(RuntimeError):
    """会话已清空、权限被撤回或本轮已被替代。"""


class ChatContext:
    """插件可自由使用真实请求、运行时与服务；不是权限沙箱。"""

    def __init__(
        self,
        service: ChatService,
        request: ChatRequest,
        runtime: ToolRuntime | None = None,
        *,
        is_valid: Callable[[], bool] | None = None,
    ) -> None:
        self.service = service
        self.request = request
        self.runtime = runtime or ToolRuntime()
        self._is_valid = is_valid
        self.providers = provider_manager
        from .memory.repository import chat_repository

        self.repository = chat_repository
        self.sessions = service.session_store
        self.model_request: LLMRequest | None = None
        self.executor: Callable[[ChatContext], Awaitable[str | LLMResponse | ChatResponse | None]] | None = None
        self.result: str | LLMResponse | ChatResponse | None = None
        self.phase = "chat"
        self.learn = bool(request.metadata.get("learn", True))
        self.stopped = False
        self.sent = False
        self.status = "pending"
        self.error: BaseException | None = None
        self.persisted: tuple[int, int | None] | None = None
        self.started = False
        self.last_provider = ""
        self.last_response: ChatResponse | None = None
        self._committed = False
        self._executing = False
        from .scheduling import GateDecision, chat_scheduling

        self.scheduling = chat_scheduling
        self.gate = GateDecision()
        self.schedule_owner = None

    @property
    def session_id(self) -> str:
        return self.request.session_id

    @property
    def text(self) -> str:
        return self.request.text

    @property
    def metadata(self) -> dict[str, Any]:
        return self.request.metadata

    def stop_event(self) -> None:
        """停止当前聊天入口后续处理，不取消工具或平台事件。"""
        self.stopped = True

    def is_stopped(self) -> bool:
        return self.stopped

    def set_result(self, result: str | LLMResponse | ChatResponse) -> None:
        self.result = result

    async def observe(self) -> None:
        """各入站渠道共用门控前扩展，避免平台与网页的接管顺序分叉。"""
        await dispatch("message", self)
        await dispatch("gate", self, self.gate)
        self.metadata["_chat_gate"] = self.gate
        self.metadata["learn"] = self.learn

    def check_current(self) -> None:
        from .work_schedule import can_reply

        epoch = self.request.accepted_epoch
        valid = epoch is None or self.service.is_current(self.session_id, epoch)
        valid = valid and (self._is_valid() if self._is_valid is not None else self.service.is_latest(self.request))
        if not valid:
            raise ChatExpired("本轮已清空或被新消息替代")
        if not can_reply(session_id=self.session_id):
            raise ChatExpired("当前会话已停止回复")

    async def ensure_current(self) -> None:
        self.check_current()
        if self.runtime.event is not None:
            if self.runtime.authorizer is None or not await self.runtime.authorizer.check("chat", self.runtime.event):
                raise ChatExpired("聊天权限已撤回")
        self.check_current()

    def history(self) -> list[dict[str, Any]]:
        """真实用户与成功发送的助手正文，不重放内部规划，也不重复当前用户消息。"""
        ids = set(self.request.source_message_ids or [self.request.message_id or self.request.dedupe_key])
        history = [
            {"role": item["role"], "content": deepcopy(item["content"])}
            for item in self.sessions.get(self.session_id)
            if item.get("role") in {"user", "assistant"}
            and (item.get("_sender_id") if item["role"] == "user" else item.get("_self_message"))
            and not item.get("_planner_internal")
            and not item.get("_reference")
            and not item.get("tool_calls")
            and not item.get("tool_call_id")
            and item.get("content")
            and not ids.intersection(item.get("_source_ids") or [])
        ]
        limit = self.history_limit()
        return history[-limit:] if limit > 0 else []

    def history_limit(self) -> int:
        from ..config import get_manager

        return history_limit(get_manager().get_system()["chat"], self.request.is_group)

    def system_text(self, *, persona: bool = True) -> str:
        from .personas import get_persona_text, effective_persona_name

        if not persona:
            return ""
        name = str(self.metadata.get("persona_name") or effective_persona_name(self.session_id))
        return get_persona_text(name)

    async def recall(self, query: str | None = None) -> str:
        from .service import _memory_block

        if query is None:
            return await _memory_block(self.request, self.text)
        from .amemorix.service import search

        return json.dumps(
            await search(self.session_id, query, user_id=self.request.user_id, group_id=self.request.group_id or ""),
            ensure_ascii=False,
        )

    async def clear_history(self, *, full: bool = False) -> bool:
        operation = self.service.reset_session if full else self.service.clear_session
        return await operation(self.session_id)

    async def append_history(
        self,
        text: str,
        *,
        user_id: str | None = None,
        user_name: str = "",
        message_id: str = "",
    ) -> None:
        """显式导入用户发言；助手最终结果应使用 submit，而不是伪造已发送记录。"""
        from uuid import uuid4

        self.check_current()
        await self.service.record_group_message(
            self.session_id,
            self.request.user_id if user_id is None else user_id,
            user_name,
            text,
            is_group=self.request.is_group,
            platform=self.request.platform,
            message_id=message_id or None,
            dedupe_key=f"import:{message_id or uuid4().hex}",
            learn=self.learn,
        )

    async def update_history(self, message_id: str, text: str) -> None:
        """按平台消息身份同步修改持久历史与内存；不影响消息顺序或学习来源。"""
        from .memory import state

        self.check_current()
        epoch = self.sessions.epoch(self.session_id)
        if state.is_ready():
            row = await self.repository.message_by_identity(self.session_id, message_id)
            if row is not None:
                await self.repository.update_message_content(self.session_id, row.id, text, generation=epoch)
        self.check_current()
        async with self.sessions.lock(self.session_id):
            self.check_current()
            for item in self.sessions.get(self.session_id):
                if message_id in (item.get("_source_ids") or []):
                    item["content"] = text

    async def expressions(self, query: str | None = None) -> str:
        from .service import _learning_reference

        return await _learning_reference(self.request, self.history(), reply_reference=query or self.text)

    async def prepare_request(
        self,
        *,
        persona: bool = True,
        memory: bool = True,
        expressions: bool = True,
        summary: bool = True,
        vision: bool = True,
    ) -> LLMRequest:
        """组合现有增强服务，不持久化临时模型上下文。"""
        from .vision import describe_images, vision_configured
        from ..config import get_manager
        from .replyer.prompt import build_persona_text
        from .context_summary import current_summary
        from .providers.routing import fallback_provider

        config = get_manager().get_system()["chat"]
        sections = []
        if persona:
            _, text = build_persona_text(self.request, config, config.get("behavior") or {})
            sections.append(text)
        if summary:
            sections.append(current_summary(self.session_id))
        if memory:
            sections.append(await self.recall())
        if expressions:
            sections.append(await self.expressions())
        content: Any = self.text
        if self.request.images:
            if vision and vision_configured(config):
                async with fallback_provider("vision", self.session_id) as provider:
                    descriptions = await describe_images(provider, self.request.images)
                content = "\n".join([self.text, *descriptions])
            else:
                content = [{"type": "text", "text": self.text}] + [
                    {"type": "image_url", "image_url": {"url": url}} for url in self.request.images
                ]
        messages = self.history() + [{"role": "user", "content": content}]
        return LLMRequest(messages=messages, system_prompt="\n\n".join(s for s in sections if s))

    async def tools(self, registry: ToolRegistry | None = None) -> tuple[set[str], list[dict[str, Any]]]:
        from .tools.executor import tool_allowed
        from .replyer.generator import allowed_tools

        if registry is None or registry is default_registry:
            runtime = self.runtime if registry is None else replace(self.runtime, registry=None)
            return await allowed_tools(runtime, proactive=is_proactive(self.request), session_id=self.session_id)
        names = {
            spec.name for spec in registry.list() if await tool_allowed(spec, self.runtime, session_id=self.session_id)
        }
        return names, [item for item in registry.to_openai_tools() if item["function"]["name"] in names]

    async def run_tool(
        self,
        name: str,
        arguments: str | dict[str, Any],
        *,
        registry: ToolRegistry | None = None,
        timeout: float | None = 120,
    ) -> dict[str, Any]:
        from .tools.types import ToolContext
        from .tools.executor import execute_tool

        await self.ensure_current()
        selected = registry if registry is not None else self.runtime.registry or default_registry
        names, _ = await self.tools(selected)
        context = ToolContext(
            self.request.user_id,
            self.request.group_id,
            self.session_id,
            self.request.platform,
            is_proactive(self.request),
            {**self.metadata, "allowed_tools": sorted(names)},
        )
        token = current_chat.set(self)
        try:
            result = await asyncio.wait_for(
                execute_tool(name, arguments, context, self.runtime, allowed_names=names, registry=selected), timeout
            )
        finally:
            current_chat.reset(token)
        await self.ensure_current()
        return result

    async def tool_message(self, call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        from .tools.result import tool_result_content

        name = call["function"]["name"]
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": name,
            "content": tool_result_content(result, session_id=self.session_id, tool_name=name),
        }

    async def llm_generate(self, request: LLMRequest, *, provider: Any = None) -> LLMResponse:
        """只调用一次模型；使用现有租约、用量和追踪，不自动记聊天历史或执行工具。"""
        from .trace_context import bind_turn, unbind_turn
        from .providers.routing import TaskProvider, candidate_providers

        await self.ensure_current()
        token = current_chat.set(self)
        trace_token = bind_turn(self.session_id, self.request.turn_id)
        try:
            if provider is not None:
                response = await provider.chat(_request=request)
                self.last_provider = str((response.provider_data or {}).get("_provider_id") or provider.name)
                await self.ensure_current()
                return response
            choices = (
                [request.provider_id]
                if request.provider_id is not None
                else candidate_providers("reply", self.session_id)
            )
            async with TaskProvider(choices, task=request.phase) as provider:
                response = await provider.chat(_request=request)
                self.last_provider = (
                    str(response.provider_data.get("_provider_id", provider.name))
                    if (response.provider_data)
                    else provider.name
                )
            await self.ensure_current()
            return response
        finally:
            unbind_turn(trace_token)
            current_chat.reset(token)

    async def tool_loop_agent(
        self,
        request: LLMRequest,
        *,
        provider: Any = None,
        initial_response: LLMResponse | None = None,
    ) -> LLMResponse:
        """标准调用、执行、回填循环；仅工作副本被追加，不改用户历史。"""
        if request.max_steps < 1:
            raise ValueError("max_steps 必须大于零")
        working = replace(request, messages=deepcopy(request.messages))
        registry = request.tool_registry or self.runtime.registry or default_registry
        for _ in range(request.max_steps):
            self.check_current()
            if working.tool_registry is not None:
                registry = working.tool_registry
            names, definitions = await self.tools(registry)
            if working.tools is None and working.tool_registry is not None:
                working.tools = definitions
            if working.tools is not None:
                working.tools = [item for item in working.tools if item["function"]["name"] in names]
            if initial_response is not None:
                response, initial_response = initial_response, None
            else:
                response = await self.llm_generate(working, provider=provider)
            if not response.has_tool_calls:
                return response
            working.messages.append(response.to_message_dict())
            advertised = {item["function"]["name"] for item in working.tools or []}
            for call in response.tool_calls:
                function = call["function"]
                if function["name"] not in advertised:
                    result = {"ok": False, "error": f"工具未提供给本轮模型: {function['name']}"}
                else:
                    result = await self.run_tool(
                        function["name"],
                        function.get("arguments", "{}"),
                        registry=working.tool_registry or registry,
                        timeout=working.tool_timeout,
                    )
                working.messages.append(await self.tool_message(call, result))
        raise RuntimeError(f"工具循环达到上限: {request.max_steps}")

    async def request_llm(self, request: LLMRequest) -> ChatResponse:
        """托管执行；聊天 hook 内复用当前轮次，不重新获取自己的轮次锁。"""
        if self._executing:
            self.metadata.pop("planner_loop", None)
            self.metadata["_direct_llm"] = False
            await self.ensure_started()
            raw = await self.tool_loop_agent(request)
            self.result = raw
            return ChatResponse(raw.content, raw=raw)
        return await self.service.execute(self, model_request=request)

    async def submit(self, result: str | LLMResponse | ChatResponse) -> ChatResponse:
        """把自行编排的最终结果交回核心，复用历史与最终发送。"""
        if self._executing:
            self.result = result
            return (
                result
                if isinstance(result, ChatResponse)
                else ChatResponse(
                    result.content if isinstance(result, LLMResponse) else result,
                    raw=result if isinstance(result, LLMResponse) else None,
                )
            )
        return await self.service.execute(self, result=result)

    def new_turn(self, instruction: str = "") -> ChatContext:
        """独立主动轮次，不把系统唤醒指令记作新的用户发言。"""
        from uuid import uuid4

        key = uuid4().hex
        request = ChatRequest(
            session_id=self.session_id,
            user_id=self.request.user_id,
            group_id=self.request.group_id,
            text=instruction,
            turn_id=key,
            dedupe_key=f"wakeup:{key}",
            trigger_kind="proactive",
            proactive=True,
            platform=self.request.platform,
            accepted_epoch=self.sessions.epoch(self.session_id),
            metadata={"continuation": True, "learn": self.learn},
        )
        context = ChatContext(self.service, request, self.runtime)
        context.schedule_owner = self.schedule_owner
        return context

    async def send(self, message: Any) -> Any:
        """进度消息；复用发送守卫，不当作最终 AI 回复落库。"""
        await self.ensure_current()
        sender = self.runtime.sender
        if sender is None:
            raise RuntimeError("当前聊天没有发送通道")
        if not await guard("send_guard", hook_context(self.request, phase="send"), kind="progress", text=str(message)):
            return None
        await self.ensure_current()
        receipt = await sender.send(message)
        await dispatch("after_send", self, message, receipt)
        return receipt

    async def ensure_started(self) -> None:
        from .replyer.prompt import strip_images, build_user_content
        from .message_context import message_metadata

        if self.started:
            return
        self.check_current()
        self.metadata["learn"] = self.learn
        if self.request.accepted_epoch is None:
            self.request.accepted_epoch = self.sessions.epoch(self.session_id)
        content = strip_images([{"role": "user", "content": build_user_content(self.request)}])[0]["content"]
        async with self.sessions.lock(self.session_id):
            self.check_current()
            if not is_continuation(self.request) and not (
                self.metadata.get("history_recorded") or self.metadata.get("history_appended")
            ):
                self.sessions.append(
                    self.session_id,
                    {
                        "role": "user",
                        "content": content,
                        "_sender_id": self.request.user_id,
                        "_source_ids": self.request.source_message_ids
                        or [self.request.message_id or self.request.dedupe_key],
                        **message_metadata(
                            timestamp=self.request.occurred_at,
                            sender_id=self.request.user_id,
                            sender_name=self.request.user_name,
                        ),
                    },
                    limit=self.history_limit(),
                )
                self.metadata["history_appended"] = True
        self.persisted = await self.service._persist_turn_start(self.request, str(content), self.request.accepted_epoch)
        self.started = True
        self.check_current()

    async def finalize(self, result: str | LLMResponse | ChatResponse) -> ChatResponse:
        """结果只提交一次；实际发送仍由现有 sender 完成。"""
        if self._committed:
            raise RuntimeError("本轮结果已提交")
        await self.ensure_started()
        raw = (
            result
            if isinstance(result, LLMResponse)
            else result.raw
            if isinstance(result, ChatResponse) and isinstance(result.raw, LLMResponse)
            else LLMResponse(content=result.text if isinstance(result, ChatResponse) else result)
        )
        response = result if isinstance(result, ChatResponse) else ChatResponse(raw.content, raw=raw)
        original = response.text
        await dispatch("reply", self, response)
        parts = response.resolve_parts(original)
        response.epoch = self.request.accepted_epoch
        response.metadata.update({"reply_parts": parts, "plugin_reply": True, "learn": self.learn})
        raw.content = response.text
        self.check_current()
        ids = await self.service._persist_turn_end(
            self.request, self.persisted, raw, self.last_provider, self.text, [], parts, original
        )
        if ids is not None:
            response.metadata["turn_item_ids"] = ids
        if self.persisted is not None:
            response.metadata["turn_db_id"] = self.persisted[0]
        response.context = self
        self._committed = True
        self.last_response = response
        return response
