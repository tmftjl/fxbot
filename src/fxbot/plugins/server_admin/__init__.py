"""超级用户的独立服务器会话及本机 shell 运维工具，私聊与群聊前缀共用同一会话。"""

from __future__ import annotations

import os
import re
import json
import asyncio
from typing import Any

from loguru import logger

from . import store
from ...core import Event, Message, BotClient, HandlerContext, MessageSegment
from .access import (
    actor_base,
    is_superuser,
    normalize_id,
    owner_allowed,
    plugin_enabled,
    event_plain_text,
    group_actor_base,
    mark_group_event,
    execution_allowed,
    owner_allowed_group,
)
from .config import blocked_reason, permission_mode, policy_switches, remember_command, skip_confirmation
from .policy import decide as decide_command
from ...config import get_manager as get_config_manager
from .executor import audit, executor
from .ui_schema import get_ui_schema
from .cc_service import (
    manager as cc_manager,
    sender_for,
    session_key,
    file_sender_for,
    image_sender_for,
    render_markdown_pngs,
)
from ...chat.media import load_images
from ...chat.tools import ToolError, ToolContext, ToolRuntime, tool
from ...permission import is_superuser_event
from ...core.plugin import PluginMetadata, on_regex
from ...utils.paths import data_dir, config_dir, startup_marker_path
from ...chat.extensions import LLMRequest, ChatContext, ChatExtension, SessionPolicy, chat_service
from ...chat.message_adapter import event_identity, adapt_message_event, image_sources_for_event

__plugin_extensions__ = {"console.config": get_ui_schema}

__plugin_meta__ = PluginMetadata(
    name="服务器运维",
    description="超级用户的独立服务器运维会话，执行 Bot 进程权限内的 shell 命令；私聊与群聊共用同一会话。",
    usage=(
        "#服务器模式 / #聊天模式（私聊）；群聊用 #服务器模式# 接对话；"
        "auto 模式下跟随请求自动执行，拿不准或命中强制确认规则时回复「确认执行」。"
        " 超级用户可用 cc <任务> 调用 Claude Code（cc new / cc stop / cc status / cc allow / cc deny）。"
    ),
    type="application",
)

_COMMAND = re.compile(r"^[#＃](服务器模式|聊天模式)\s*$")
_GROUP_COMMAND = re.compile(r"^[#＃]服务器模式[#＃]\s*")
_extension = ChatExtension("server_admin")
_PROMPT_MARKER = "[服务器运维环境]\n"


def _resolve(bot, event, base: str) -> str | None:
    """模式开启后固定派生会话，即使撤权或插件停用也不退回普通聊天泄露历史。"""
    del bot, event
    if ":private:" not in base:
        return None
    return store.server_session(base) if store.enabled(base) else None


def _policy(session_id: str) -> SessionPolicy | None:
    """服务器域禁止 Focus 且不受作息限制；共享、防抖、输入状态与分段由控制台开关决定。"""
    if not store.is_server_session(session_id):
        return None
    flags = policy_switches()
    return SessionPolicy(
        allow_focus=False,
        allow_sharing=flags["allow_sharing"],
        allow_debounce=flags["allow_debounce"],
        allow_typing=flags["allow_typing"],
        allow_split=flags["allow_split"],
        allow_typo=False,
        allow_rest_time=True,
    )


def _guard(context, details):
    """关闭、撤权和退出立即拒绝服务器会话生成及发送，普通会话不受影响。"""
    del details
    if store.is_server_session(context.session_id):
        return (
            bool(context.user_id)
            and plugin_enabled()
            and is_superuser(store.base_session(context.session_id))
            and store.enabled(context.session_id)
        )
    return None


def _tools(context, details):
    """shell 和确认只在服务器会话可发现；确认工具只在有待确认命令时出现。"""
    name = details["tool_name"]
    if store.is_server_session(context.session_id):
        allowed = bool(_guard(context, {}))
        if name == "server_exec":
            return allowed
        if name == "server_confirm":
            return allowed and bool(executor.pending(context.session_id))
        return name in {"tool_search", "read_tool_result", "query_memory", "query_person_profile", "send_file"}
    return False if name in {"server_exec", "server_confirm"} else None


def _runtime_facts() -> str:
    """本进程的启动时刻和目录：PID 与 ps 启动时间在 execv 重启后不变，只有这里能看出重启。"""
    try:
        record = json.loads(startup_marker_path().read_text(encoding="utf-8"))
        head = (
            f"本进程 pid={record.get('pid')}，启动于 {record.get('started_at')}"
            f"（started_epoch={record.get('started_epoch')}）。"
        )
    except Exception:
        head = "本进程的启动记录不存在，说明还没加载写启动记录的版本。"
    return (
        f"{head}#重启/#更新 用 execv 原地换进程镜像，PID 和 ps 的启动时间都不变，"
        "别用它们判断有没有重启过。\n"
        f"数据目录 {data_dir()}，配置目录 {config_dir()}。"
    )


def _services_note() -> str:
    """服务清单文件路径；有内容就一并注入。"""
    path = data_dir("server_admin") / "services.md"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        content = ""
    return f"服务清单 {path}\n{content}".strip()


def _prompt(context: ChatContext, request: LLMRequest) -> None:
    """提示工作环境和确认方式，不覆盖用户通过 #人格 设置的绑定。"""
    if not store.is_server_session(context.session_id) or request.phase not in {"reply", "planner"}:
        return
    request.messages[:] = [
        message
        for message in request.messages
        if not (message.get("role") == "system" and str(message.get("content", "")).startswith(_PROMPT_MARKER))
    ]
    if skip_confirmation():
        note = (
            "这是服务器运维会话。命令用 server_exec 执行，未结束用 job_id 查，别重复提交。"
            "全部命令免确认已开启：直接执行，破坏性操作先讲清影响。被禁用规则拒绝就如实说，别绕过。"
        )
    elif permission_mode() == "auto":
        note = (
            "这是服务器运维会话。命令用 server_exec 执行，未结束用 job_id 查，别重复提交。"
            "权限模式 auto：与当前要求一致的日常命令会直接执行；命中强制确认规则、或与要求不一致时，"
            "工具会返回待确认状态，此时先把命令展示给对方，等回复「确认执行」后再调 server_confirm，否则 cancel。"
            "被禁用规则拒绝就如实说，别绕过。"
        )
    else:
        note = (
            "这是服务器运维会话。命令用 server_exec 执行，未结束用 job_id 查，别重复提交。"
            "需要确认的命令先展示，请对方回复「确认执行」后再调 server_confirm，否则 cancel。"
            "被禁用规则拒绝就如实说，别绕过。"
        )
    if job := executor.pending(context.session_id):
        command = job["command"]
        tail = "…" if len(command) > 600 else ""
        note += f"\n待确认命令：\n{command[:600]}{tail}"
    content = f"{_PROMPT_MARKER}{note}\n{_runtime_facts()}\n{_services_note()}"
    position = next(
        (index for index, message in enumerate(request.messages) if message.get("role") != "system"),
        len(request.messages),
    )
    request.messages.insert(position, {"role": "system", "content": content})


async def _observe(event: str, context, details) -> None:
    """清空服务器对话时撤销确认并停止相关命令。"""
    del details
    if event == "session_clear_start" and store.is_server_session(context.session_id):
        executor.cancel(context.session_id)


# 模块只注册纯回调；实际命令任务由触发它的 Application 创建和回收。
_extension.on("session_resolver", _resolve)
_extension.on("session_policy", _policy)
_extension.on("request_guard", _guard)
_extension.on("send_guard", _guard)
_extension.on("tool_guard", _tools)
_extension.on("llm_request", _prompt)
_extension.on("observer", _observe, events={"session_clear_start"})


async def _rule(context: HandlerContext) -> bool:
    """仅拦截超级用户私聊的模式命令。"""
    return owner_allowed(context.bot, context.event)


mode_cmd = on_regex(
    _COMMAND.pattern,
    name="mode",
    display_name="服务器模式",
    level="superuser",
    scene="private",
    rule=_rule,
    priority=3,
    block=True,
)


def _describe(result: dict[str, Any]) -> str:
    """确认前展示完整命令与工作目录，不截断待审批的实际内容。"""
    return (
        f"要执行这条命令吗？\n\n{result['command']}\n\n"
        f"工作目录：{result['cwd']}\n"
        "它跑在 Bot 的系统权限下，可能改动或删除数据。\n"
        "回复「确认执行」同意本次执行；回复「确认执行 永久」同意并以后不再询问这条命令；或要求取消。"
    )


def _request_text(runtime: ToolRuntime) -> str:
    """取本次真实请求文本供分类器比对；群聊入口去掉模式前缀。"""
    try:
        text = event_plain_text(runtime.event)
    except Exception:
        return ""
    return _GROUP_COMMAND.sub("", text, count=1).strip()


def _message_key(runtime: ToolRuntime) -> str:
    """本轮触发消息的幂等键，用来禁止同一轮自我确认。"""
    if runtime.event is None or runtime.bot is None:
        return ""
    try:
        return event_identity(runtime.bot, runtime.event).dedupe_key
    except Exception:
        return ""


async def handle_mode(bot: BotClient, event: Event) -> str:
    """执行私聊模式命令，返回预定义提示，运行时异常由入口记录后静默结束。"""
    if not owner_allowed(bot, event):
        return ""
    matched = _COMMAND.fullmatch(event_plain_text(event))
    if matched is None:
        return ""
    base = actor_base(bot, event)
    session_id = store.server_session(base)
    if matched[1] == "服务器模式":
        store.set_mode(base, True)
        audit("mode_enabled", session_id, user_id=str(event.user_id))
        return "服务器模式已开启。后续私聊使用独立人格、历史和会话记忆。命令以 Bot 进程权限执行，并非安全沙箱。"
    store.set_mode(base, False)
    executor.cancel(session_id)
    audit("mode_disabled", session_id)
    return "已退出服务器模式，待确认及运行任务已取消。后续消息回到普通私聊；服务器历史保留。"


@mode_cmd.handle()
async def _handle(context: HandlerContext) -> None:
    """私聊命令入口，故障只留日志，不把执行异常发给用户。"""
    try:
        message = await handle_mode(context.bot, context.event)
    except Exception:
        logger.opt(exception=True).warning("[ServerAdmin] 模式命令失败")
        await context.finish()
        return
    if message:
        await context.finish(message)


async def _group_rule(context: HandlerContext) -> bool:
    """只接受群聊里由超级用户触发的前缀。"""
    return owner_allowed_group(context.bot, context.event)


group_cmd = on_regex(
    r"^[#＃]服务器模式[#＃]",
    name="group_mode",
    display_name="群聊服务器模式",
    level="superuser",
    scene="group",
    rule=_group_rule,
    priority=3,
    block=True,
)


async def handle_group_turn(bot: BotClient, event: Event, context: HandlerContext) -> str:
    """把群里的前缀消息投进同一私聊服务器会话，回复仍发回本群。"""
    if not owner_allowed_group(bot, event):
        return ""
    text = _GROUP_COMMAND.sub("", event_plain_text(event), count=1).strip()
    if not text:
        return "请在 #服务器模式# 后输入内容"
    base = group_actor_base(bot, event)
    session_id = store.server_session(base)
    if not store.enabled(session_id):
        return "服务器会话未开启，请先在私聊发送 #服务器模式"
    identity = event_identity(bot, event)
    if not chat_service.session_store.mark_seen(session_id, identity.dedupe_key):
        return ""
    await chat_service.ensure_hydrated(session_id, is_group=False)
    owner = normalize_id(event.user_id) or ""
    request = await adapt_message_event(bot, event, trigger_kind="private")
    # 会话共用私聊域：历史、人格和记忆都按私聊服务器会话处理，只是发送目标仍是本群。
    request.session_id = session_id
    request.group_id = None
    request.text = text
    request.trigger_seq = chat_service.session_store.next_seq(session_id, owner)
    request.trigger_owner = owner
    request.accepted_epoch = chat_service.session_store.epoch(session_id)
    request.activity_seq = chat_service.session_store.activity(session_id)
    mark_group_event(event)
    services = context.app_services
    runtime = ToolRuntime(
        bot=bot,
        event=event,
        sender=context,
        application=getattr(services, "application", None),
        authorizer=getattr(services, "authorizer", None),
    )
    await chat_service.process_and_send(request, runtime)
    return ""


@group_cmd.handle()
async def _handle_group(context: HandlerContext) -> None:
    """群聊入口，故障只留日志。"""
    try:
        message = await handle_group_turn(context.bot, context.event, context)
    except Exception:
        logger.opt(exception=True).warning("[ServerAdmin] 群聊服务器命令失败")
        await context.finish()
        return
    if message:
        await context.finish(message)


@tool(
    name="server_exec",
    permission="server_admin:mode",
    title="服务器命令",
    deferred=True,
    description=(
        "在服务器运维会话中执行任意本机 shell 命令。提交 command 执行；任务未结束时用 job_id 查询，"
        "或 cancel=true 取消。危险命令会先把完整命令展示给对方，对方明确同意后再调用 server_confirm。"
        "完成后总结结果，不直接复制日志，不向用户展示 job_id 或工具名。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "完整 shell 命令，不与 job_id 同时提供"},
            "cwd": {"type": "string", "description": "绝对工作目录，默认 Bot 当前目录"},
            "job_id": {"type": "string", "description": "提交后查询任务结果的编号"},
            "cancel": {"type": "boolean", "default": False},
        },
    },
)
async def server_exec(
    context: ToolContext, runtime: ToolRuntime, command: str = "", cwd: str = "", job_id: str = "", cancel: bool = False
) -> dict:
    """工具本身校验真实入口（私聊或群聊前缀）、超级用户、插件开关、派生会话及调用者身份。"""

    def authorize() -> bool:
        """运行中沿用真实事件身份，重新读取当前权限及开关。"""
        return execution_allowed(context.session_id, runtime.bot, runtime.event, context.user_id, context.group_id)

    if runtime.application is None or not context.user_id or not authorize():
        raise ToolError("仅允许超级用户在已开启的服务器会话使用", code="server_admin_denied")
    try:
        if cancel:
            return {"cancelled": executor.cancel(context.session_id)}
        if job_id:
            if command:
                raise ValueError("查询任务不能同时提交命令")
            return executor.query(context.session_id, job_id)
        if reason := blocked_reason(command):
            try:
                audit("blocked", context.session_id, user_id=str(context.user_id), command=command, rule=reason)
            except Exception:
                logger.opt(exception=True).error("[ServerAdmin] 黑名单拦截审计失败")
            raise ToolError(f"命令命中服务器禁用规则，已拒绝执行：{reason[:120]}", code="server_admin_denied")
        decision = await decide_command(
            session_id=context.session_id,
            command=command,
            cwd=cwd or os.getcwd(),
            request_text=_request_text(runtime),
        )
        try:
            audit(
                "policy",
                context.session_id,
                user_id=str(context.user_id),
                command=command,
                decision=decision.action,
                source=decision.source,
                reason=decision.reason,
            )
        except Exception:
            logger.opt(exception=True).error("[ServerAdmin] 权限决策审计失败")
        result = executor.submit(
            context.session_id,
            command,
            cwd,
            authorize,
            application=runtime.application,
            auto_run=decision.auto_run,
        )
        if result["status"] == "running":
            job = executor.jobs[result["job_id"]]
            # 简单查询通常立即完成，直接把结果交给 Planner，长任务仍使用 job_id。
            if job.task is not None:
                await asyncio.wait({job.task}, timeout=1.0)
            result = executor.query(context.session_id, job.id)
        if result["status"] == "pending":
            # 确认必须让真人先看到精确执行内容，不能只依赖模型转述。
            if runtime.sender is None:
                executor.cancel(context.session_id)
                raise ToolError("当前缺少确认通道")
            try:
                await runtime.sender.send(_describe(result))
                executor.presented(context.session_id, result["job_id"], _message_key(runtime))
            except BaseException:
                executor.cancel(context.session_id)
                raise
        return result
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        # 原样告诉模型哪种情况失败（尤其是「已有待确认任务」），避免它误报提交成功或反复重提。
        raise ToolError(str(exc) or "参数无效或目录不存在", code="server_admin_invalid") from None
    except PermissionError:
        raise ToolError("执行权限不足或确认已失效", code="server_admin_denied") from None


@tool(
    name="server_confirm",
    permission="server_admin:mode",
    title="确认服务器命令",
    description=(
        "对方在会话里明确同意执行当前待确认命令后调用，无参数。"
        "仅接受「确认执行」「确认」「同意执行」「执行」「跑吧」「行」或 yes/confirm 的完整回复；"
        "对方说「确认执行 永久」「永久允许」「以后都允许」时同样可以调用，代码会把这条完整命令记入永久允许。"
        "提问、否定、含糊和其他内容会被代码拒绝，不能替对方决定。"
        "调用后命令立即执行并返回结果，直接向对方汇报。"
    ),
    parameters={"type": "object", "properties": {}},
)
async def server_confirm(context: ToolContext, runtime: ToolRuntime) -> dict:
    """确认真人已看到命令，并拒绝同一事件的自我确认。"""
    if (
        runtime.application is None
        or not context.user_id
        or not execution_allowed(context.session_id, runtime.bot, runtime.event, context.user_id, context.group_id)
    ):
        raise ToolError("仅允许超级用户在已开启的服务器会话使用", code="server_admin_denied")
    reply = _GROUP_COMMAND.sub("", event_plain_text(runtime.require_event()), count=1).strip()
    try:
        result = executor.confirm(context.session_id, _message_key(runtime), reply)
    except ValueError:
        raise ToolError("没有待确认的命令，或确认已过期", code="server_admin_invalid") from None
    except PermissionError:
        raise ToolError("未获得明确确认，或执行权限已失效", code="server_admin_denied") from None
    if executor.is_permanent_reply(reply):
        remembered = remember_command(str(result.get("command") or ""))
        try:
            audit(
                "permanent_allow",
                context.session_id,
                user_id=str(context.user_id),
                command=str(result.get("command") or ""),
                saved=remembered,
            )
        except Exception:
            logger.opt(exception=True).error("[ServerAdmin] 永久允许审计失败")
    job = executor.jobs[result["job_id"]]
    if job.task is not None:
        try:
            await asyncio.shield(job.task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not job.task.cancelled():
                raise
    if job.status == "cancelled" or not execution_allowed(
        context.session_id, runtime.bot, runtime.event, context.user_id, context.group_id
    ):
        raise ToolError("执行已取消或权限已失效", code="server_admin_denied")
    return job.result()


# ---- Claude Code（cc 前缀，仅超级用户，私聊群聊都可用） ----

_CC_HELP = (
    "Claude Code 命令：\n"
    "cc <任务>        交给 Claude Code 执行\n"
    "cc new          重置会话（清上下文）\n"
    "cc clear        清空工作目录（会话保留）\n"
    "cc stop         打断当前任务\n"
    "cc status       查看会话状态\n"
    "cc sessions     列出可恢复会话\n"
    "cc resume <id>  恢复指定会话\n"
    "cc doctor       检查 ACP 依赖\n"
    "cc 允许 / cc 允许 永久 / cc 拒绝（或 allow / allow always / deny）   审批工具调用"
)


async def _cc_rule(context: HandlerContext) -> bool:
    """只允许超级用户使用，私聊和群聊都放行。"""
    try:
        system = get_config_manager().get_system()["system"]
        return is_superuser_event(system, context.event)
    except Exception:
        logger.opt(exception=True).debug("[ServerAdmin] Claude Code 超级用户校验失败")
        return False


async def _send_cc_help(context: HandlerContext) -> None:
    """优先发送与 #服务器帮助 同款的帮助图。"""
    try:
        from ..help import help_image

        image = await help_image("server_admin")
    except Exception:
        image = b""
    if image:
        await context.send(Message([MessageSegment.image(image)]))
        return
    await context.send(_CC_HELP)


CC_PATTERN = r"^[cC]{2}[ \t　 ]*(\S[\s\S]*)$"

cc_cmd = on_regex(
    CC_PATTERN,
    name="cc",
    display_name="Claude Code",
    level="superuser",
    scene="all",
    priority=6,
    block=True,
    rule=_cc_rule,
)


@cc_cmd.handle()
async def _handle_cc(context: HandlerContext) -> None:
    """cc 前缀入口：控制命令或提交任务。"""
    try:
        groups = context.match.groups if isinstance(context.match.groups, tuple) else ()
        text = str(groups[0] or "").strip() if groups else ""
        key = session_key(context.event)
        lowered = text.lower()
        sources = await image_sources_for_event(context.bot, context.event)
        images = await load_images(sources) if sources else []
        if not text and not images:
            return
        if not text:
            lowered = ""
        if lowered in {"new", "reset"}:
            await context.send(await cc_manager.reset(key))
            return
        if lowered == "clear":
            await context.send(await cc_manager.clear(key))
            return
        if lowered == "stop":
            await context.send(await cc_manager.stop(key))
            return
        if lowered == "status":
            await context.send(cc_manager.status(key))
            return
        if lowered == "engine" or lowered.startswith(("engine ", "eng ")) or lowered == "eng":
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            await context.send(cc_manager.engines(key) if not arg else await cc_manager.switch_engine(key, arg))
            return
        if lowered == "doctor":
            markdown = cc_manager.doctor_markdown()
            try:
                images = await render_markdown_pngs(markdown, "ACP")
            except Exception:
                images = []
            if images:
                for image in images:
                    await context.send(Message([MessageSegment.image(image)]))
            else:
                await context.send(markdown)
            return
        if lowered in {"sessions", "session"}:
            await context.send(await cc_manager.sessions(key))
            return
        if lowered in {"queue", "q"}:
            await context.send(cc_manager.queue_info(key))
            return
        if lowered.startswith(("dequeue ", "dq ")):
            parts = text.split(maxsplit=1)
            await context.send(await cc_manager.dequeue(key, parts[1] if len(parts) > 1 else ""))
            return
        if lowered == "resume":
            await context.send("用法：cc resume <序号|session_id>")
            return
        if lowered.startswith("resume "):
            sid = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            await context.send(await cc_manager.resume(key, sid))
            return
        if text.startswith("允许") or lowered.startswith(("allow", "approve")):
            kind = "allow_always" if ("永久" in text or "always" in lowered) else "allow_once"
            await context.send(cc_manager.resolve(key, kind))
            return
        if text.startswith("拒绝") or lowered in {"deny", "reject"}:
            await context.send(cc_manager.resolve(key, "reject_once"))
            return
        started, message = await cc_manager.start(
            key,
            text,
            sender_for(context.bot, context.event),
            image_sender_for(context.bot, context.event),
            file_sender_for(context.bot, context.event),
            images=images,
        )
        if message:
            await context.send(message)
    except Exception:
        logger.opt(exception=True).warning("[ServerAdmin] Claude Code 命令失败")
