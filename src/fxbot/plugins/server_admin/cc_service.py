"""Claude Code 的 ACP 会话：子进程、流式输出和聊天内的工具审批。"""

from __future__ import annotations

import re
import json
import time
import base64
import shutil
import asyncio
import threading
from typing import Any, Callable, Awaitable
from pathlib import Path
from contextlib import suppress
from collections import deque

from acp import PROTOCOL_VERSION, spawn_agent_process
from loguru import logger
from acp.schema import (
    DeniedOutcome,
    ToolCallStart,
    AllowedOutcome,
    PermissionOption,
    TextContentBlock,
    ToolCallProgress,
    AgentMessageChunk,
    AgentThoughtChunk,
    ImageContentBlock,
    ClientCapabilities,
    RequestPermissionResponse,
)
from acp.interfaces import Client

from . import cc_store, cc_render
from ...core import Message, BotClient, Capability, SessionTarget, MessageSegment
from .config import (
    CC_ENGINES,
    cc_engine,
    cc_workdir,
    cc_idle_seconds,
    cc_keep_context,
    cc_render_scale,
    cc_tool_display,
    cc_agent_command,
    cc_busy_behavior,
    cc_output_format,
    cc_show_thinking,
    cc_approve_timeout,
    cc_permission_mode,
)

PERMISSION_TIMEOUT = 300.0
FLUSH_INTERVAL = 2.0
MAX_MESSAGE_CHARS = 3500
MAX_BUFFER_CHARS = 60_000
AUTO_IMAGE_THRESHOLD = 600
MAX_SESSIONS = 16
SPAWN_FAIL_THRESHOLD = 3
SPAWN_COOLDOWN = 60.0
MAX_REFERENCED_ATTACHMENTS = 8
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
_FILE_PATH_RE = re.compile(r"(?:\b[A-Za-z]:[\\/]|~|(?<![:/\w])/)[\w./\\-]+\.\w{1,8}")

Sender = Callable[[str], Awaitable[None]]
ImageSender = Callable[[bytes], Awaitable[None]]
FileSender = Callable[[Path, str], Awaitable[None]]

_pid_lock = threading.Lock()
_spawn_failures = 0
_spawn_block_until = 0.0


def _pid_path() -> Path:
    from ...utils.paths import data_dir

    return data_dir("claude_code") / "spawned_pids.json"


def _load_pids() -> dict[str, str]:
    try:
        raw = json.loads(_pid_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}


def _save_pids(data: dict[str, str]) -> None:
    with _pid_lock:
        try:
            _pid_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.opt(exception=True).debug("[ServerAdmin] 保存 Claude Code pid 记录失败")


def _record_pid(pid: int) -> None:
    data = _load_pids()
    data[str(pid)] = "claude-agent-acp"
    _save_pids(data)


def _forget_pid(pid: int) -> None:
    data = _load_pids()
    if data.pop(str(pid), None) is not None:
        _save_pids(data)


def _reap_orphans() -> int:
    """回收上次运行遗留的适配器进程。"""
    import psutil

    data = _load_pids()
    if not data:
        return 0
    reaped = 0
    for raw_pid in list(data):
        try:
            pid = int(raw_pid)
            process = psutil.Process(pid)
            cmdline = " ".join(process.cmdline())
        except (ValueError, psutil.Error):
            continue
        if "claude-agent-acp" not in cmdline:
            continue
        children = []
        with suppress(psutil.Error):
            children = process.children(recursive=True)
        for child in children:
            with suppress(psutil.Error):
                child.kill()
        with suppress(psutil.Error):
            process.kill()
        reaped += 1
    _save_pids({})
    return reaped


def _check_spawn_cooldown() -> None:
    if _spawn_block_until > time.monotonic():
        raise RuntimeError("Claude Code 连续启动失败，请稍后再试")


def _note_spawn_failure() -> None:
    global _spawn_failures, _spawn_block_until
    _spawn_failures += 1
    if _spawn_failures >= SPAWN_FAIL_THRESHOLD:
        _spawn_block_until = time.monotonic() + SPAWN_COOLDOWN
        _spawn_failures = 0


def _note_spawn_success() -> None:
    global _spawn_failures, _spawn_block_until
    _spawn_failures = 0
    _spawn_block_until = 0.0


class _Client(Client):
    """把 ACP 回调转给当前会话。"""

    def __init__(self, session: "CcSession") -> None:
        self._session = session

    async def session_update(self, session_id: str, update: object, **_: object) -> None:
        await self._session.on_update(update)

    async def request_permission(
        self,
        session_id: str,
        tool_call: object,
        options: list[PermissionOption],
        **_: object,
    ) -> RequestPermissionResponse:
        return await self._session.on_permission(tool_call, options)


class CcSession:
    """一个超级用户一条 ACP 会话，跨私聊和群聊共用。"""

    def __init__(self, key: str, workdir: Path, engine: str = "claude") -> None:
        self.key = key
        self.workdir = workdir
        self.engine = engine
        self.conn: Any = None
        self.acp_session: str = ""
        self._cm: Any = None
        self._proc: Any = None
        self._lock = asyncio.Lock()
        self._buffer: list[str] = []
        self._buffer_chars = 0
        self._should_flush = False
        self._seen_tools: set[tuple[str, str]] = set()
        self._pendings: list[tuple[asyncio.Future[str | None], list[PermissionOption]]] = []
        self._think: list[str] = []
        self._resume_requested = ""
        self._sender: Sender | None = None
        self._notice: Sender | None = None
        self._image_sender: ImageSender | None = None
        self._file_sender: FileSender | None = None
        self._force_resume = False
        self._markdown: list[str] = []
        self._tool_notes: list[tuple[str, str, str]] = []
        self._started = 0.0
        self._flush_task: asyncio.Task | None = None
        self.task: asyncio.Task | None = None
        self.busy = False
        self.last_active = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.conn is not None and bool(self.acp_session)

    def attach(
        self,
        sender: Sender,
        notice: Sender | None = None,
        image: ImageSender | None = None,
        file: FileSender | None = None,
    ) -> None:
        """记住正文、提示、图片与文件的回复通道。"""
        self._sender = sender
        self._notice = notice
        self._image_sender = image
        self._file_sender = file

    async def _deliver(self, sender: Sender | None, text: str) -> None:
        text = text.strip()
        if not text or sender is None:
            return
        while text:
            chunk, text = text[:MAX_MESSAGE_CHARS], text[MAX_MESSAGE_CHARS:].lstrip()
            try:
                await sender(chunk)
            except Exception:
                logger.opt(exception=True).debug("[ServerAdmin] Claude Code 消息发送失败")
                return

    async def _send(self, text: str) -> None:
        """发送正文。"""
        await self._deliver(self._sender, text)

    async def _notify(self, text: str) -> None:
        """发送进度、错误和审批提示。"""
        await self._deliver(self._notice or self._sender, text)

    async def _send_image(self, data: bytes) -> None:
        """发送一张图片。"""
        if self._image_sender is None:
            await self._notify(f"[图片 {len(data) // 1024} KiB]")
            return
        try:
            await self._image_sender(data)
        except Exception:
            logger.opt(exception=True).debug("[ServerAdmin] Claude Code 图片发送失败")

    async def ensure(self) -> None:
        """按需启动适配器并建立 ACP 会话。"""
        if self.alive:
            return
        async with self._lock:
            if self.alive:
                return
            _check_spawn_cooldown()
            command = cc_agent_command(self.engine)
            if not command:
                raise RuntimeError("没有可用的 ACP 启动命令")
            command[0] = shutil.which(command[0]) or command[0]
            self.workdir.mkdir(parents=True, exist_ok=True)
            cm: Any = None
            conn: Any = None
            proc: Any = None
            try:
                cm = spawn_agent_process(_Client(self), command[0], *command[1:], cwd=str(self.workdir))
                conn, proc = await cm.__aenter__()
                self._cm, self._proc, self.conn = cm, proc, conn
                _record_pid(proc.pid)
                await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                )
                await self._open_session(conn)
            except Exception:
                _note_spawn_failure()
                if proc is not None:
                    _forget_pid(proc.pid)
                if conn is not None:
                    with suppress(Exception):
                        await conn.close()
                if cm is not None:
                    with suppress(Exception):
                        await cm.__aexit__(None, None, None)
                self._cm = None
                self._proc = None
                self.conn = None
                raise
            _note_spawn_success()
            self._flush_task = asyncio.create_task(self._flush_loop(), name=f"cc-flush-{self.key}")
            logger.info(f"[ServerAdmin] Claude Code 会话已建立: {self.acp_session}")

    async def _open_session(self, conn: Any) -> None:
        """按配置恢复保留的会话，失败或未开启时新建。"""
        session_id = ""
        keep = cc_keep_context() or self._force_resume
        self._force_resume = False
        requested = self._resume_requested
        self._resume_requested = ""
        if keep:
            sticky = cc_store.get(self.key)
            if sticky:
                session_id = await self._resume(conn, sticky)
        if not session_id:
            response = await conn.new_session(cwd=str(self.workdir), mcp_servers=[])
            session_id = str(response.session_id)
            if requested:
                await self._notify(f"会话 {requested} 恢复失败，已新建会话。")
        self.acp_session = session_id
        if cc_keep_context():
            cc_store.put(self.key, session_id)

    async def _resume(self, conn: Any, session_id: str) -> str:
        """先试 resume，再试 load；都不支持就返回空串走新会话。"""
        for method in ("resume_session", "load_session"):
            call = getattr(conn, method, None)
            if call is None:
                continue
            try:
                response = await call(
                    session_id=session_id,
                    cwd=str(self.workdir),
                    mcp_servers=[],
                )
            except Exception:
                logger.opt(exception=True).debug(f"[ServerAdmin] Claude Code {method} 失败")
                continue
            restored = str(getattr(response, "session_id", "") or session_id)
            logger.info(f"[ServerAdmin] Claude Code 会话已恢复: {restored}")
            return restored
        return ""

    async def run_prompt(self, text: str, images: list[str] | None = None) -> None:
        """执行一轮 prompt；文本模式流式发送，图片模式结束后统一渲染。"""
        self.busy = True
        self.last_active = time.monotonic()
        self._markdown.clear()
        self._think.clear()
        self._tool_notes.clear()
        self._buffer.clear()
        self._buffer_chars = 0
        self._started = time.monotonic()
        failed = False
        response: Any = None
        try:
            await self.ensure()
            response = await self.conn.prompt(
                session_id=self.acp_session,
                prompt=_prompt_blocks(text, images),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = True
            logger.opt(exception=True).warning("[ServerAdmin] Claude Code 执行失败")
            await self._notify(f"Claude Code 执行失败：{exc}")
        finally:
            if not failed:
                await self._deliver_output(response)
            self.busy = False
            self.last_active = time.monotonic()
            if failed:
                await self.close(drop_native=False)

    async def _deliver_output(self, response: Any) -> None:
        """按输出格式发送纯文本或渲染后的图片。"""
        markdown = "".join(self._markdown).strip()
        tool_text = "\n".join(
            f"🔧 {title}" + (f"\n  {summary}" if summary else "") for _, title, summary in self._tool_notes
        )
        fmt = cc_output_format()
        total = len(markdown) + len(tool_text)
        if fmt == "text" or (fmt == "auto" and total < AUTO_IMAGE_THRESHOLD):
            await self.flush()
            if tool_text:
                await self._send(tool_text)
            if markdown:
                await self._send(markdown)
            elif not tool_text:
                await self._send("（Claude Code 没有输出）")
            await self._send_attachments(markdown, tool_text)
            return

        blocks: list[Any] = []
        for kind, title, summary in self._tool_notes:
            body = f"{title}\n\n{summary}" if summary else title
            blocks.append(
                cc_render.ToolBlock(
                    kind="tool",
                    body=body,
                    tool_kind=kind or "other",
                    title=title,
                )
            )
        if self._think:
            blocks.append(cc_render.ChatBlock("think", " ".join(self._think)))
        if markdown:
            blocks.append(cc_render.ChatBlock("agent_md", markdown))
        footer = _usage_footer(response)
        if footer:
            blocks.append(cc_render.ChatBlock("usage_footer", footer))
        if not blocks:
            await self._send("（Claude Code 没有输出）")
            return
        context = cc_render.ImageContext(
            engine_display="Claude Code",
            model_label=None,
            elapsed_sec=time.monotonic() - self._started,
            icon_url=cc_render.engine_icon_url("claude"),
        )
        body = cc_render.build_html_body(blocks, context)
        pngs = await cc_render.render_to_pngs(
            body,
            max_width=720,
            scale=cc_render_scale(),
        )
        if not pngs:
            # 渲染失败时退回纯文本，不丢结果
            await self._send(markdown or tool_text or "（渲染失败）")
            return
        for png in pngs:
            await self._send_image(png)
        await self._send_attachments(markdown, tool_text)

    async def _send_attachments(self, *texts: str) -> None:
        """把正文里提到、且位于工作目录内的文件发给用户。"""
        paths = _collect_attachments("\n".join(texts), self.workdir)
        for path in paths:
            if path.suffix.lstrip(".").lower() in _IMAGE_EXTS:
                try:
                    await self._send_image(path.read_bytes())
                except OSError:
                    continue
                continue
            if self._file_sender is None:
                await self._notify(f"[文件] {path}")
                continue
            try:
                await self._file_sender(path, path.name)
            except Exception:
                logger.opt(exception=True).debug("[ServerAdmin] Claude Code 文件发送失败")
                await self._notify(f"[文件] {path}")

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL)
                await self.flush()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.opt(exception=True).debug("[ServerAdmin] Claude Code 刷新输出失败")

    async def flush(self) -> None:
        """把累计的正文发出去。"""
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._buffer_chars = 0
        if text.strip():
            await self._send(text)

    async def on_update(self, update: object) -> None:
        """处理 ACP 的流式更新。"""
        if isinstance(update, AgentMessageChunk):
            content = update.content
            if getattr(content, "type", None) == "image":
                data = _image_bytes(content)
                if data:
                    await self._send_image(data)
                return
            text = _text_of(content)
            if text:
                if cc_output_format() == "text":
                    self._buffer.append(text)
                    self._buffer_chars += len(text)
                    if self._buffer_chars >= MAX_BUFFER_CHARS:
                        await self.flush()
                else:
                    self._markdown.append(text)
            return
        if isinstance(update, AgentThoughtChunk):
            if not cc_show_thinking():
                return
            text = _text_of(update.content)
            if not text:
                return
            if cc_output_format() == "text":
                self._buffer.append(text)
            else:
                self._think.append(text)
            return
        if isinstance(update, (ToolCallStart, ToolCallProgress)):
            if cc_tool_display() == "off":
                return
            status = str(getattr(update, "status", "") or "")
            tool_id = str(getattr(update, "tool_call_id", "") or "")
            kind = str(getattr(update, "kind", "") or "other")
            title = str(getattr(update, "title", "") or getattr(update, "kind", "") or "").strip()
            if not title or status not in {"pending", "in_progress"}:
                return
            marker = (tool_id, status)
            if marker in self._seen_tools:
                return
            self._seen_tools.add(marker)
            summary = _tool_summary(update) if cc_tool_display() == "full" else ""
            self._tool_notes.append((kind, title, summary))
            if cc_output_format() == "text":
                await self.flush()
                detail = f"\n  {summary}" if summary else ""
                await self._notify(f"🔧 {title}{detail}")

    async def on_permission(
        self,
        tool_call: object,
        options: list[PermissionOption],
    ) -> RequestPermissionResponse:
        """按 cc 自己的配置自动放行，或把审批发到聊天里等待回复。"""
        if cc_permission_mode() == "allow":
            chosen = next((item for item in options if item.kind == "allow_always"), None)
            chosen = chosen or next((item for item in options if item.kind == "allow_once"), None)
            if chosen is None:
                return RequestPermissionResponse(outcome=DeniedOutcome())
            return RequestPermissionResponse(outcome=AllowedOutcome(option_id=chosen.option_id))

        await self.flush()
        title = str(getattr(tool_call, "title", "") or getattr(tool_call, "kind", "") or "工具调用").strip()
        kinds = "、".join(sorted({item.kind for item in options}))
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        entry = (future, list(options))
        self._pendings.append(entry)
        await self._notify(
            f"Claude Code 请求权限：{title}\n可选：{kinds}\n"
            "回复 cc 允许 / cc 允许 永久 / cc 拒绝（或 allow / allow always / deny）。"
        )
        timeout = float(cc_approve_timeout())
        try:
            option_id = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            option_id = None
        finally:
            with suppress(ValueError):
                self._pendings.remove(entry)
        if option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome())
        return RequestPermissionResponse(outcome=AllowedOutcome(option_id=option_id))

    def resolve(self, kind: str) -> str:
        """按先后顺序处理最早的一条待审批请求。"""
        entry = next(((f, o) for f, o in self._pendings if not f.done()), None)
        if entry is None:
            return "当前没有待审批的请求。"
        future, options = entry
        option = next((item for item in options if item.kind == kind), None)
        if option is None:
            offered = "、".join(sorted({item.kind for item in options})) or "无"
            return f"agent 没有提供 {kind} 选项，可选：{offered}。"
        future.set_result(option.option_id)
        return "已允许。" if kind.startswith("allow") else "已拒绝。"

    async def cancel(self) -> str:
        """打断当前任务，会话保留；待审批请求按拒绝处理。"""
        if not self.busy or not self.alive:
            return "当前没有正在执行的任务。"
        for future, _ in self._pendings:
            if not future.done():
                future.set_result(None)
        with suppress(Exception):
            await self.conn.cancel(session_id=self.acp_session)
        return "已请求打断。"

    async def close(self, *, drop_native: bool = True) -> None:
        """关闭 ACP 会话和子进程；drop_native 决定是否丢弃保留的会话 id。"""
        kept = self.acp_session
        self.acp_session = ""
        flush_task, self._flush_task = self._flush_task, None
        if flush_task is not None:
            flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await flush_task
        for future, _ in self._pendings:
            if not future.done():
                future.set_result(None)
        self._pendings.clear()
        conn, self.conn = self.conn, None
        if conn is not None:
            with suppress(Exception):
                await conn.close()
        proc, self._proc = self._proc, None
        if proc is not None:
            _forget_pid(proc.pid)
        cm, self._cm = self._cm, None
        if cm is not None:
            with suppress(Exception):
                await cm.__aexit__(None, None, None)
        if drop_native:
            cc_store.drop(self.key)
        elif kept:
            cc_store.put(self.key, kept)


def _collect_attachments(text: str, workdir: Path) -> list[Path]:
    """从输出里提取工作目录内的绝对路径文件。"""
    root = workdir.resolve()
    seen: set[Path] = set()
    found: list[Path] = []
    for raw in _FILE_PATH_RE.findall(text or ""):
        if len(found) >= MAX_REFERENCED_ATTACHMENTS:
            break
        try:
            path = Path(raw).expanduser().resolve()
            if not path.is_file() or not path.is_relative_to(root):
                continue
            size = path.stat().st_size
        except OSError:
            continue
        ext = path.suffix.lstrip(".").lower()
        limit = MAX_IMAGE_BYTES if ext in _IMAGE_EXTS else MAX_FILE_BYTES
        if size > limit or path in seen:
            continue
        seen.add(path)
        found.append(path)
    return found


def _wipe(path: Path) -> int:
    """清空目录内容但保留目录本身；返回删除的条目数。"""
    removed = 0
    for child in path.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _prompt_blocks(text: str, images: list[str] | None = None) -> list[Any]:
    """把文字和 data URL 图片组成 ACP prompt 内容块。"""
    blocks: list[Any] = [TextContentBlock(text=(text or "请查看图片。"))]
    for url in (images or [])[:4]:
        try:
            header, payload = str(url).split(",", 1)
        except ValueError:
            continue
        if not header.startswith("data:") or not payload:
            continue
        mime = header[5:].split(";", 1)[0] or "image/png"
        blocks.append(ImageContentBlock(data=payload, mime_type=mime))
    return blocks


def _image_bytes(content: object) -> bytes | None:
    """解码 ACP 图片块。"""
    payload = str(getattr(content, "data", "") or "")
    if not payload:
        return None
    try:
        return base64.b64decode(payload)
    except (ValueError, TypeError):
        return None


def _usage_footer(response: Any) -> str:
    """把 PromptResponse 的 usage 和 stop_reason 压成一行。"""
    usage = getattr(response, "usage", None)
    parts: list[str] = []
    if usage is not None:

        def _num(name: str) -> int | None:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        input_tokens = _num("input_tokens") or _num("prompt_tokens")
        output_tokens = _num("output_tokens") or _num("completion_tokens")
        if input_tokens is not None:
            parts.append(f"in {input_tokens}")
        if output_tokens is not None:
            parts.append(f"out {output_tokens}")
    stop = getattr(response, "stop_reason", None)
    if stop:
        parts.append(str(stop))
    return " · ".join(parts)


def _tool_summary(update: object) -> str:
    """从工具调用内容里取一段简短摘要。"""
    parts: list[str] = []
    for item in getattr(update, "content", None) or []:
        block = getattr(item, "content", None)
        if block is None and isinstance(item, dict):
            block = item.get("content")
        if isinstance(block, dict):
            text = str(block.get("text") or "")
        else:
            text = _text_of(block) if block is not None else ""
        if text:
            parts.append(text)
    return " ".join(" ".join(parts).split())[:160]


def _text_of(content: object) -> str:
    """从内容块里取文本。"""
    return str(getattr(content, "text", "") or "")


class CcManager:
    """按用户管理 ACP 会话、排队、引擎选择和容量。"""

    def __init__(self) -> None:
        self._sessions: dict[str, CcSession] = {}
        self._queues: dict[str, deque[Any]] = {}
        self._drains: dict[str, asyncio.Task] = {}
        self._sweeper: asyncio.Task | None = None
        self._reaped = False

    def _reap_once(self) -> None:
        """首次使用时回收上次残留的适配器进程。"""
        if self._reaped:
            return
        self._reaped = True
        try:
            _reap_orphans()
        except Exception:
            logger.opt(exception=True).debug("[ServerAdmin] 回收 Claude Code 孤儿进程失败")

    def _start_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="cc-sweeper")

    def _session_key(self, key: str) -> str:
        """用户键 + 当前引擎，作为内部会话和存储键。"""
        return f"{key}|{cc_engine(key)}"

    def get(self, key: str) -> CcSession:
        """取得或创建用户当前引擎的会话。"""
        self._reap_once()
        skey = self._session_key(key)
        session = self._sessions.get(skey)
        if session is None:
            session = CcSession(skey, cc_workdir(skey), cc_engine(key))
            self._sessions[skey] = session
        self._start_sweeper()
        return session

    def existing(self, key: str) -> CcSession | None:
        return self._sessions.get(self._session_key(key))

    async def _ensure_capacity(self, keep_key: str) -> None:
        """超过会话上限时回收最久未活动的空闲会话。"""
        while len(self._sessions) >= MAX_SESSIONS:
            candidates = [
                (session.last_active, key)
                for key, session in self._sessions.items()
                if key != keep_key and not session.busy and not session._pendings
            ]
            if not candidates:
                return
            _, victim = min(candidates)
            session = self._sessions.pop(victim)
            self._queues.pop(victim, None)
            await self._stop_drain(victim)
            await session.close(drop_native=not cc_keep_context())

    async def _stop_drain(self, key: str) -> None:
        task = self._drains.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _drain_key(self, key: str) -> str:
        return self._session_key(key)

    async def start(
        self,
        key: str,
        text: str,
        sender: Sender,
        image: ImageSender | None = None,
        file: FileSender | None = None,
        images: list[str] | None = None,
    ) -> tuple[bool, str]:
        """提交一轮任务；按配置排队或拒绝。"""
        session = self.get(key)
        skey = session.key
        queue = self._queues.setdefault(skey, deque())
        item = (text, images, sender, image, file)
        draining = self._drains.get(skey)
        if session.busy or queue or (draining is not None and not draining.done()):
            if cc_busy_behavior() == "reject":
                return False, "上一轮还在执行，先 cc stop 或等它结束。"
            queue.append(item)
            return True, f"已排队，前面还有 {len(queue) - 1} 条。"
        queue.append(item)
        await self._ensure_capacity(skey)
        self._drains[skey] = asyncio.create_task(self._drain(skey), name=f"cc-prompt-{skey}")
        return True, f"已交给 {session.engine}，结果会在这条会话里陆续发出。"

    async def _drain(self, skey: str) -> None:
        """串行消费该用户的队列。"""
        queue = self._queues.get(skey)
        try:
            while queue:
                text, images, sender, image, file = queue.popleft()
                session = self._sessions.get(skey)
                if session is None:
                    continue
                session.attach(sender, image=image, file=file)
                try:
                    await session.run_prompt(text, images)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.opt(exception=True).warning("[ServerAdmin] 队列执行失败")
        finally:
            if self._drains.get(skey) is asyncio.current_task():
                self._drains.pop(skey, None)

    def queue_info(self, key: str) -> str:
        """列出当前引擎的排队任务。"""
        skey = self._session_key(key)
        queue = self._queues.get(skey) or deque()
        if not queue:
            return "当前没有排队任务。"
        lines = [f"排队：{len(queue)} 条"]
        for index, item in enumerate(queue, start=1):
            text = str(item[0] or "").strip()
            preview = " ".join(text.split())[:40] or "[图片]"
            lines.append(f"{index}. {preview}")
        lines.append("取消：cc dequeue <序号>")
        return "\n".join(lines)

    async def dequeue(self, key: str, index: str) -> str:
        """按序号取消一条排队任务。"""
        skey = self._session_key(key)
        queue = self._queues.get(skey)
        if not queue:
            return "当前没有排队任务。"
        try:
            position = int(index)
        except ValueError:
            return "用法：cc dequeue <序号>（先用 cc queue 查看）"
        if position < 1 or position > len(queue):
            return f"序号超出范围，当前排队 {len(queue)} 条。"
        del queue[position - 1]
        return f"已取消第 {position} 条排队任务，剩余 {len(queue)} 条。"

    async def clear(self, key: str) -> str:
        """只清当前引擎的工作目录内容，会话和历史保留。"""
        skey = self._session_key(key)
        session = self._sessions.get(skey)
        if session is not None and session.busy:
            return "还在执行，先 cc stop 再清空工作目录。"
        target = session.workdir if session is not None else cc_workdir(skey)
        removed = await asyncio.to_thread(_wipe, target)
        return f"已清空工作目录中的 {removed} 项。" if removed else "工作目录已经是空的。"

    def doctor(self) -> str:
        """检查 ACP 依赖是否可用。"""
        rows = [
            ("node", shutil.which("node")),
            ("npx", shutil.which("npx")),
            ("claude", shutil.which("claude")),
            ("codex", shutil.which("codex")),
        ]
        lines = ["ACP 体检"]
        for name, path in rows:
            lines.append(f"{name}: {path or '未找到'}")
        for engine in CC_ENGINES:
            command = cc_agent_command(engine)
            lines.append(f"{engine}: " + (" ".join(command) if command else "未配置"))
        return "\n".join(lines)

    def engines(self, key: str) -> str:
        """列出引擎和当前选择。"""
        current = cc_engine(key)
        lines = ["可用引擎："]
        for engine in CC_ENGINES:
            mark = "*" if engine == current else " "
            lines.append(f"{mark} {engine}")
        lines.append("切换：cc engine <name>")
        return "\n".join(lines)

    async def switch_engine(self, key: str, name: str) -> str:
        """切换引擎；当前会话忙碌时拒绝。"""
        target = name.strip().lower()
        if not target or target not in CC_ENGINES:
            return "用法：cc engine <" + "|".join(CC_ENGINES) + ">"
        if target == cc_engine(key):
            return f"已经在使用 {target}。"
        skey = self._session_key(key)
        session = self._sessions.get(skey)
        draining = self._drains.get(skey)
        if session is not None and (session.busy or session._pendings or self._queues.get(skey)):
            return "还有任务在跑或排队，先 cc stop 再切换引擎。"
        if draining is not None and not draining.done():
            return "还有任务在跑，先 cc stop 再切换引擎。"
        if session is not None:
            await session.close(drop_native=not cc_keep_context())
            self._sessions.pop(skey, None)
            self._queues.pop(skey, None)
        cc_store.put_engine(key, target)
        return f"已切换到 {target}，下一条 cc 会启动新的引擎会话。"

    async def sessions(self, key: str) -> str:
        """列出当前引擎可恢复的 ACP 会话。"""
        skey = self._session_key(key)
        session = self._sessions.get(skey)
        lines: list[str] = [f"引擎：{cc_engine(key)}"]
        if session is not None and session.alive:
            try:
                response = await session.conn.list_sessions(cwd=str(session.workdir))
                for item in list(response.sessions)[:10]:
                    mark = "*" if item.session_id == session.acp_session else " "
                    title = (item.title or "").strip()
                    lines.append(f"{mark} {item.session_id} {title}".rstrip())
            except Exception:
                logger.opt(exception=True).debug("[ServerAdmin] 会话列表失败")
        if len(lines) == 1:
            sticky = cc_store.get(skey)
            lines.append(f"本地保留：{sticky or '无'}")
        lines.append("恢复：cc resume <session id>")
        return "\n".join(lines)

    async def resume(self, key: str, session_id: str) -> str:
        """指定当前引擎下一次使用的 ACP 会话。"""
        sid = session_id.strip()
        if not sid:
            return "用法：cc resume <session id>（先用 cc sessions 查看）"
        skey = self._session_key(key)
        session = self._sessions.get(skey)
        if session is not None and session.busy:
            return "还在执行，先 cc stop 再恢复会话。"
        if session is not None and session.alive:
            await session.close(drop_native=True)
        session = self.get(key)
        cc_store.put(skey, sid)
        session._force_resume = True
        session._resume_requested = sid
        return f"下一次 cc 会恢复到会话 {sid}。"

    async def reset(self, key: str) -> str:
        """关闭当前引擎会话并清空上下文。"""
        skey = self._session_key(key)
        self._queues.pop(skey, None)
        await self._stop_drain(skey)
        session = self._sessions.pop(skey, None)
        if session is None:
            cc_store.drop(skey)
            return "还没有会话，下一条 cc 会新建。"
        await session.close(drop_native=True)
        return f"{session.engine} 会话已重置。"

    async def stop(self, key: str) -> str:
        """打断当前任务并清空排队。"""
        skey = self._session_key(key)
        self._queues.pop(skey, None)
        session = self._sessions.get(skey)
        return "还没有会话。" if session is None else await session.cancel()

    def resolve(self, key: str, kind: str) -> str:
        session = self._sessions.get(self._session_key(key))
        return "还没有会话。" if session is None else session.resolve(kind)

    def status(self, key: str) -> str:
        skey = self._session_key(key)
        session = self._sessions.get(skey)
        if session is None:
            return f"还没有会话（引擎 {cc_engine(key)}）。发送 cc <任务> 开始。"
        if session.busy:
            state = "执行中"
        elif session._pendings:
            state = "等待审批"
        else:
            state = "空闲"
        queue_len = len(self._queues.get(skey) or ())
        lines = [
            f"引擎：{session.engine}",
            f"状态：{state}",
            f"排队：{queue_len}",
            f"工作目录：{session.workdir}",
            f"ACP 会话：{session.acp_session or '未启动'}",
            f"保留会话：{cc_store.get(skey) or '无'}",
            f"空闲回收保留上下文：{'开' if cc_keep_context() else '关'}",
        ]
        return "\n".join(lines)

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                limit = cc_idle_seconds()
                now = time.monotonic()
                for skey, session in list(self._sessions.items()):
                    if session.busy or session._pendings or self._queues.get(skey):
                        continue
                    if now - session.last_active < limit:
                        continue
                    self._sessions.pop(skey, None)
                    self._queues.pop(skey, None)
                    await session.close(drop_native=not cc_keep_context())
            except asyncio.CancelledError:
                return
            except Exception:
                logger.opt(exception=True).debug("[ServerAdmin] 空闲回收失败")


manager = CcManager()


def session_key(event: object) -> str:
    """同一用户跨私聊群聊共用一条会话。"""
    return SessionTarget(
        str(getattr(event, "platform", "") or ""),
        str(getattr(event, "bot_id", "") or ""),
        "private",
        str(getattr(event, "user_id", "") or ""),
    ).key()


def sender_for(bot: BotClient, event: object) -> Sender:
    """构造把文本发回本次会话的发送器。"""
    target = getattr(event, "target", None)

    async def _send(text: str) -> None:
        await bot.send(target, Message.text(text))

    return _send


def file_sender_for(bot: BotClient, event: object) -> FileSender:
    """构造把工作目录文件发回本次会话的发送器。"""
    target = getattr(event, "target", None)

    async def _send_file(path: Path, name: str) -> None:
        if Capability.FILE not in bot.capabilities:
            raise RuntimeError("当前平台不支持发送文件")
        await bot.capabilities.require(Capability.FILE).send_file(target, path, name)

    return _send_file


def image_sender_for(bot: BotClient, event: object) -> ImageSender:
    """构造把图片发回本次会话的发送器。"""
    target = getattr(event, "target", None)

    async def _send_image(data: bytes) -> None:
        await bot.send(target, Message([MessageSegment.image(data)]))

    return _send_image
