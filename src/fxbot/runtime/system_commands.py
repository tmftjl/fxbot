"""核心进程控制命令：重启、关机、更新并重启。"""

from __future__ import annotations

import os
import sys
import json
import time
import atexit
import signal
import asyncio
from collections.abc import Callable

from loguru import logger

from ..core import Event, Message, Capability, SessionTarget, HandlerContext
from ..config import ConfigManager
from .processes import terminate_process
from ..utils.paths import data_dir, project_root
from ..core.matcher import bind_matchers
from ..core.commands import on_regex
from ..core.permission import Permission
from ..core.application import Application
from ..permission.identity import is_superuser_event

_FLAG_FILE = data_dir("system") / "restart_flag.json"
_FLAG_TTL_SECONDS = 600
_UPDATE_TIMEOUT_SECONDS = 180
_GIT_QUERY_TIMEOUT_SECONDS = 15
_PROCESS_CLOSE_TIMEOUT_SECONDS = 5
_REPORT_TIMEOUT_SECONDS = 5
_UP_TO_DATE_MARKERS = ("already up to date", "already up-to-date", "已经是最新")

_PENDING_ARGV: list[str] | None = None


def superuser_check(configuration: ConfigManager):
    """超级用户可以是完整私聊键，也可以只填平台用户 ID。"""

    async def check(event: Event) -> bool:
        return is_superuser_event(configuration.get_system()["system"], event)

    return check


def _restart_argv() -> list[str]:
    """当前进程始终可以用 ``python -m fxbot`` 重新拉起。"""
    return [sys.executable, "-m", "fxbot", *(str(item) for item in sys.argv[1:])]


def _set_pending_restart() -> None:
    global _PENDING_ARGV
    _PENDING_ARGV = _restart_argv()


def reexec_if_pending() -> None:
    """关闭流程收尾时原地替换进程镜像；取走即清空，只会执行一次。"""
    global _PENDING_ARGV
    argv, _PENDING_ARGV = _PENDING_ARGV, None
    if not argv:
        return
    logger.warning("正在重新拉起 FxBot: {}", " ".join(argv))
    try:
        os.execv(argv[0], argv)
    except OSError:
        logger.opt(exception=True).error("重新拉起进程失败，进程将正常结束")


def _request_exit() -> None:
    """投递 SIGINT，让 uvicorn 走完整的优雅关闭流程。"""
    signal.raise_signal(signal.SIGINT)


def _save_restart_target(event: Event, message: str) -> None:
    """记录重启后要通知的会话，进程重启后由启动任务发送。"""
    payload = {
        "target": event.target.key(),
        "platform": event.platform,
        "bot_id": event.bot_id,
        "message": message,
        "created_at": time.time(),
    }
    try:
        _FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FLAG_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.opt(exception=True).warning("保存重启通知目标失败")


def _read_restart_target() -> dict | None:
    """读取未过期的重启通知；坏文件直接清除。"""
    try:
        data = json.loads(_FLAG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.opt(exception=True).warning("读取重启通知目标失败")
        _clear_restart_target()
        return None
    if not isinstance(data, dict):
        _clear_restart_target()
        return None
    created_at = data.get("created_at")
    keys = (data.get("target"), data.get("platform"), data.get("bot_id"), data.get("message"))
    if (
        not all(isinstance(value, str) and value for value in keys)
        or not isinstance(created_at, (int, float))
        or time.time() - created_at > _FLAG_TTL_SECONDS
    ):
        _clear_restart_target()
        return None
    return data


def _clear_restart_target() -> None:
    try:
        _FLAG_FILE.unlink(missing_ok=True)
    except OSError:
        logger.opt(exception=True).warning("清除重启通知目标失败")


async def _notify_after_restart(application: Application) -> None:
    """等待目标 Bot 重新上线后发送一次重启成功通知。"""
    data = _read_restart_target()
    if data is None:
        return
    try:
        target = SessionTarget.from_key(data["target"])
    except ValueError:
        _clear_restart_target()
        return
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        for bot in application.bots:
            if (bot.platform, bot.bot_id) != (data["platform"], data["bot_id"]):
                continue
            await asyncio.sleep(1)
            try:
                await bot.send(target, Message.text(data["message"]))
            except Exception:
                logger.opt(exception=True).warning("重启成功通知发送失败")
            finally:
                _clear_restart_target()
            return
        await asyncio.sleep(1)
    logger.warning("重启后等待 Bot 上线超时，已放弃发送通知")
    _clear_restart_target()


async def _noop() -> None:
    """资源释放占位。"""


def _start_notifier(application: Application):
    async def start() -> None:
        application.spawn(_notify_after_restart(application), name="restart-notifier")

    return start


def _truncate(text: str, limit: int = 1200) -> str:
    clean = text.strip()
    return clean if len(clean) <= limit else clean[-limit:]


def _already_up_to_date(output: str) -> bool:
    normalized = output.lower()
    return any(marker in normalized for marker in _UP_TO_DATE_MARKERS)


async def _run_git(*args: str, timeout: float = _GIT_QUERY_TIMEOUT_SECONDS) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(project_root()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )
    exchange = asyncio.create_task(process.communicate(), name="git.communicate")
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(exchange), timeout=timeout)
    finally:
        if not exchange.done():
            try:
                terminate_process(process)
                await asyncio.wait_for(exchange, timeout=_PROCESS_CLOSE_TIMEOUT_SECONDS)
            finally:
                if not exchange.done():
                    exchange.cancel()
                await asyncio.gather(exchange, return_exceptions=True)
    output = "\n".join(part.decode("utf-8", errors="replace").strip() for part in (stdout, stderr) if part).strip()
    assert process.returncode is not None
    return process.returncode, output


async def _git_head() -> str:
    code, output = await _run_git("rev-parse", "HEAD")
    return output if code == 0 else ""


async def _git_pull(on_success: Callable[[], None]) -> tuple[bool, str, list[str]]:
    """只做快进拉取，避免在运行中的服务器上产生合并提交。"""
    try:
        async with asyncio.timeout(_UPDATE_TIMEOUT_SECONDS):
            before = await _git_head()
            code, output = await _run_git("pull", "--ff-only", timeout=_UPDATE_TIMEOUT_SECONDS)
    except OSError as error:
        return False, str(error), []
    except TimeoutError:
        return False, "git 更新流程超时", []
    if code != 0:
        return False, output or f"git pull 失败，退出码 {code}", []
    on_success()
    # pull 成功已经改变了运行代码；可选的日志读取失败不能取消后续重启。
    try:
        async with asyncio.timeout(_GIT_QUERY_TIMEOUT_SECONDS):
            after = await _git_head()
            if not before or not after or before == after:
                return True, output or "Already up to date.", []
            code, log = await _run_git("log", "--reverse", "--pretty=format:%B%x1e", f"{before}..{after}")
            logs = [item.strip() for item in log.split("\x1e") if item.strip()] if code == 0 else []
    except (OSError, TimeoutError):
        logger.opt(exception=True).warning("代码更新成功，但读取提交日志失败；仍将重启")
        logs = ["提交日志读取失败，代码更新已完成。"]
    return True, output or "Already up to date.", logs


async def _send_report(context: HandlerContext, lines: list[str]) -> None:
    if Capability.FORWARD_SEND in context.bot.capabilities:
        await context.bot.capabilities.require(Capability.FORWARD_SEND).send_forward(
            context.event.target, [Message.text(line) for line in lines]
        )
        return
    text = "\n".join(lines)
    await context.send(text if len(text) <= 2000 else text[:1999] + "…")


async def _restart(context: HandlerContext) -> None:
    logger.warning("超级用户触发 Bot 重启: {}", context.event.user_id)
    try:
        await context.send("收到重启指令，正在重启 FxBot...")
    except Exception:
        logger.opt(exception=True).warning("发送重启提示失败")
    _save_restart_target(context.event, "✅ FxBot 重启成功")
    _set_pending_restart()
    await asyncio.sleep(0.5)
    _request_exit()


async def _shutdown(context: HandlerContext) -> None:
    logger.warning("超级用户触发 Bot 关闭: {}", context.event.user_id)
    try:
        await context.send("收到关机指令，FxBot 即将关闭...")
    except Exception:
        logger.opt(exception=True).warning("发送关闭提示失败")
    await asyncio.sleep(0.5)
    _request_exit()


async def _update(context: HandlerContext) -> None:
    logger.warning("超级用户触发 Bot 更新: {}", context.event.user_id)
    await context.send("正在尝试更新 FxBot，请稍等片刻。")
    restart_ready = False

    def prepare_restart() -> None:
        nonlocal restart_ready
        _save_restart_target(context.event, "✅ FxBot 更新重启成功")
        _set_pending_restart()
        restart_ready = True

    try:
        ok, output, logs = await _git_pull(prepare_restart)
        if not ok:
            logger.error("git pull 失败: {}", _truncate(output))
            await _send_report(context, ["❌ FxBot 更新失败！", "git pull 返回错误，完整信息已记录到后台日志。"])
            return
        status = "本次无更新内容" if _already_up_to_date(output) else "更新完成"
        try:
            await asyncio.wait_for(
                _send_report(context, [f"✅ FxBot {status}！", *logs]), timeout=_REPORT_TIMEOUT_SECONDS
            )
        except Exception:
            logger.opt(exception=True).warning("更新成功通知未完成，仍执行重启")
    finally:
        if restart_ready:
            _request_exit()


def install(application: Application, configuration: ConfigManager) -> None:
    """注册核心进程命令；不属于插件，也不读取插件权限存储。"""
    atexit.register(reexec_if_pending)
    application.resource(_start_notifier(application), _noop)
    owner = Permission.for_event(superuser_check(configuration))
    definitions = []
    for name, label, pattern, handler in (
        ("restart", "重启", r"^[#＃]重启$", _restart),
        ("shutdown", "关机", r"^[#＃]关机$", _shutdown),
        ("update", "更新并重启", r"^[#＃]更新$", _update),
    ):
        matcher = on_regex(pattern, name=name, display_name=label, priority=1, block=True, permission=owner)
        matcher.handle()(handler)
        definitions.append(matcher.snapshot())
    bind_matchers(application, "system", tuple(definitions))
