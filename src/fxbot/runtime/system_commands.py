"""核心管理命令：重启、关机、更新，以及按命令维度的拉黑/拉白。"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import atexit
import shutil
import signal
import asyncio
import hashlib
from copy import deepcopy
from pathlib import Path
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
from ..permission.storage import get_storage

_FLAG_FILE = data_dir("system") / "restart_flag.json"
_FLAG_TTL_SECONDS = 600
_UPDATE_TIMEOUT_SECONDS = 180
_GIT_QUERY_TIMEOUT_SECONDS = 15
_PROCESS_CLOSE_TIMEOUT_SECONDS = 5
_REPORT_TIMEOUT_SECONDS = 5
_DOCKER_APP = Path("/app")
_DEPS_FILES = ("pyproject.toml", "uv.lock")
_DEPS_STAMP_FILE = data_dir("system") / "deps.sha256"
_UP_TO_DATE_MARKERS = ("already up to date", "already up-to-date", "已经是最新")

_PENDING_ARGV: list[str] | None = None


def superuser_check(configuration: ConfigManager):
    """超级用户可以是完整私聊键，也可以只填平台用户 ID。"""

    async def check(event: Event) -> bool:
        return is_superuser_event(configuration.get_system()["system"], event)

    return check


def _docker_deployment() -> bool:
    """判断进程是否运行在容器中。"""
    return os.path.exists("/.dockerenv")


def _docker_repo() -> Path | None:
    """返回容器内挂载的仓库目录；没有挂载时返回 None。"""
    repo = Path("/repo")
    return repo if (repo / ".git").exists() else None


def _git_root() -> Path:
    """容器内优先使用挂载仓库，其他部署使用项目根目录。"""
    return _docker_repo() or project_root()


def _deps_fingerprint(repo: Path) -> str | None:
    """依赖文件哈希；读不到依赖文件时返回 None，按“需要同步”处理。"""
    digest = hashlib.sha256()
    try:
        for name in _DEPS_FILES:
            digest.update((repo / name).read_bytes())
            digest.update(b"\0")
    except OSError:
        return None
    return digest.hexdigest()


def _deps_changed(repo: Path) -> bool:
    """与上次成功同步的指纹比较；没有指纹（首次或上次失败）视为需要同步。"""
    fingerprint = _deps_fingerprint(repo)
    if fingerprint is None:
        return True
    try:
        return _DEPS_STAMP_FILE.read_text(encoding="utf-8").strip() != fingerprint
    except OSError:
        return True


def _write_deps_stamp(repo: Path) -> None:
    """同步成功后记录依赖指纹，放在数据目录里，容器重启也不会丢。"""
    fingerprint = _deps_fingerprint(repo)
    if fingerprint is None:
        return
    try:
        _DEPS_STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DEPS_STAMP_FILE.write_text(fingerprint, encoding="utf-8")
    except OSError:
        logger.opt(exception=True).warning("写入依赖指纹失败")


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


async def _run_command(*args: str, cwd: Path, timeout: float) -> tuple[int, str]:
    """等待自有子进程；超时或取消时终止进程树并收完输出。"""
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )
    exchange = asyncio.create_task(process.communicate(), name=f"{args[0]}.communicate")
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


async def _run_git(*args: str, timeout: float = _GIT_QUERY_TIMEOUT_SECONDS) -> tuple[int, str]:
    return await _run_command("git", *args, cwd=_git_root(), timeout=timeout)


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


async def _sync_docker_dependencies() -> tuple[bool, str]:
    """把挂载仓库的依赖文件同步进运行目录，并在容器内更新虚拟环境。"""
    repo = _docker_repo()
    if repo is None:
        return False, "未挂载仓库目录"
    try:
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copy2(repo / name, _DOCKER_APP / name)
        code, output = await _run_command(
            "uv",
            "sync",
            "--frozen",
            "--no-dev",
            "--inexact",
            cwd=_DOCKER_APP,
            timeout=_UPDATE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return False, "依赖同步超时"
    except OSError as error:
        return False, str(error)
    return code == 0, output


async def _sync_project_dependencies(repo: Path) -> tuple[bool, str]:
    """非容器部署直接在本项目环境里同步依赖；没装 uv 时跳过并由用户自行管理。"""
    try:
        code, output = await _run_command(
            "uv",
            "sync",
            "--frozen",
            "--inexact",
            cwd=repo,
            timeout=_UPDATE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        logger.warning("未找到 uv，跳过依赖同步: {}", repo)
        return True, "未找到 uv，跳过依赖同步"
    except TimeoutError:
        return False, "依赖同步超时"
    except OSError as error:
        return False, str(error)
    return code == 0, output


async def _send_report(context: HandlerContext, lines: list[str]) -> None:
    if Capability.FORWARD_SEND in context.bot.capabilities:
        await context.bot.capabilities.require(Capability.FORWARD_SEND).send_forward(
            context.event.target, [Message.text(line) for line in lines]
        )
        return
    text = "\n".join(lines)
    await context.send(text if len(text) <= 2000 else text[:1999] + "…")


_MIN_TARGET_DIGITS = 5
_TARGET_PATTERN = rf"(?<!\d)\d{{{_MIN_TARGET_DIGITS},}}(?!\d)"
# 「拉黑」后面贴着命令名也可以；排除「踢」开头的群管拉黑踢，避免抢占原命令。
_BLACKLIST_PATTERN = r"^[#＃]拉黑(?!踢)\s*(.*)"
_UNBLACKLIST_PATTERN = r"^[#＃]拉白\s*(.*)"


class _CommandLookupError(ValueError):
    """目标命令无法唯一解析，直接回给超级用户。"""


def _plugin_name(application: Application, namespace: str) -> str:
    """插件显示名；核心命名空间没有插件元数据时退回内部名。"""
    plugin = application.plugin_metadata.get(namespace)
    return str(plugin.name) if plugin is not None else namespace


def _command_label(application: Application, namespace: str, command: str) -> str:
    """用「插件显示名:命令显示名（内部键）」展示，便于核对。"""
    display = str(application.command_metadata.get(namespace, {}).get(command, command))
    return f"{_plugin_name(application, namespace)}:{display}（{namespace}:{command}）"


def _resolve_command(application: Application, spec: str) -> tuple[str, str]:
    """按命令显示名或内部名解析唯一的插件命令，拒绝整个插件。"""
    key = spec.strip().lstrip("#＃/").strip().lower()
    if not key:
        raise _CommandLookupError("请指定命令名，例如：#拉黑 点歌 @某人")
    found: dict[tuple[str, str], None] = {}
    plugin_keys: set[str] = set()
    for namespace, commands in application.command_metadata.items():
        plugin_name = _plugin_name(application, namespace)
        prefixes = {namespace.lower(), plugin_name.strip().lower()}
        plugin_keys.update(item for item in prefixes if item)
        for name, display in commands.items():
            display = str(display).strip()
            labels = {name.lower(), display.lower(), f"{namespace.lower()}:{name.lower()}"}
            for prefix in prefixes:
                if prefix:
                    labels.add(f"{prefix}:{name.lower()}")
                    labels.add(f"{prefix}:{display.lower()}")
            if key in labels:
                found[(namespace, name)] = None
    if not found:
        if key in plugin_keys:
            raise _CommandLookupError(f"「{spec.strip()}」是插件，请指定具体命令；不能整个插件拉黑或拉白。")
        raise _CommandLookupError(
            f"没有找到命令「{spec.strip()}」。可以用 #拉黑 插件:命令 @某人，例如 #拉黑 娱乐:今日运势 @某人"
        )
    if len(found) > 1:
        options = "、".join(_command_label(application, *item) for item in found)
        raise _CommandLookupError(f"「{spec.strip()}」匹配到多个命令：{options}。请用 插件:命令 指定。")
    return next(iter(found))


def _split_targets(event: Event, text: str) -> tuple[list[str], str]:
    """@ 与回复目标不进入纯文本，因此位置无关；文本里的长数字按 QQ 号处理。"""
    targets = [
        str(segment.data["user_id"])
        for segment in event.message.segments
        if segment.type == "mention" and segment.data.get("user_id") is not None
    ]
    if event.reply is not None and event.reply.user_id:
        targets.append(str(event.reply.user_id))
    targets.extend(re.findall(_TARGET_PATTERN, text))
    bot = str(event.bot_id)
    unique = [user for user in dict.fromkeys(targets) if user and user != bot]
    rest = re.sub(_TARGET_PATTERN, " ", text)
    return unique, " ".join(rest.split())


async def _moderate_command(application: Application, context: HandlerContext, *, remove: bool) -> None:
    """按单个命令维护用户黑名单；两个方向共用一个入口。"""
    targets, spec = _split_targets(context.event, context.match.group(1))
    try:
        namespace, command = _resolve_command(application, spec)
    except _CommandLookupError as exc:
        await context.send(str(exc))
        return
    label = _command_label(application, namespace, command)
    if not targets:
        await context.send("请 @ 目标用户、回复目标消息，或者填写至少 5 位数字的 QQ 号。")
        return
    try:
        storage = get_storage()
        data = deepcopy(storage.load())
        node = data.get("sub_plugins", {}).get(namespace)
        if not isinstance(node, dict) or not isinstance(node.get("top"), dict):
            await context.send(f"「{label}」不经过权限配置，无法单独拉黑或拉白。")
            return
        commands = node.get("commands")
        entry = commands.get(command) if isinstance(commands, dict) else None
        if not isinstance(entry, dict):
            await context.send(f"权限配置里没有「{label}」的命令条目，无法单独拉黑或拉白。")
            return
        blacklist = entry.setdefault("blacklist", {})
        users = blacklist.setdefault("users", []) if isinstance(blacklist, dict) else None
        if not isinstance(users, list):
            await context.send(f"「{label}」的黑名单用户列表格式不正确，请在后台权限页修正。")
            return
        changed: list[str] = []
        for user in targets:
            if remove:
                if user in users:
                    users.remove(user)
                    changed.append(user)
            elif user not in users:
                users.append(user)
                changed.append(user)
        if not changed:
            detail = "本来就不在黑名单里" if remove else "已经在黑名单里"
            await context.send(f"「{label}」无需{'解除拉黑' if remove else '拉黑'}：{'、'.join(targets)}{detail}。")
            return
        storage.save(data)
    except Exception:
        logger.opt(exception=True).error("保存命令黑名单失败: {} -> {}", namespace, command)
        await context.send(f"保存「{label}」的权限配置失败，请查看后台日志。")
        return
    await context.send(f"已对「{label}」{'解除拉黑' if remove else '拉黑'}：{'、'.join(changed)}。")


async def _restart(context: HandlerContext) -> None:
    logger.warning("超级用户触发 Bot 重启: {}", context.event.user_id)
    try:
        await context.send("FxBot 正在重启。")
    except Exception:
        logger.opt(exception=True).warning("发送重启提示失败")
    _save_restart_target(context.event, "FxBot 重启完成")
    _set_pending_restart()
    await asyncio.sleep(0.5)
    _request_exit()


async def _shutdown(context: HandlerContext) -> None:
    logger.warning("超级用户触发 Bot 关闭: {}", context.event.user_id)
    try:
        await context.send("FxBot 正在关闭。")
    except Exception:
        logger.opt(exception=True).warning("发送关闭提示失败")
    await asyncio.sleep(0.5)
    _request_exit()


async def _update(context: HandlerContext) -> None:
    logger.warning("超级用户触发 Bot 更新: {}", context.event.user_id)
    docker = _docker_deployment()
    if docker and _docker_repo() is None:
        await context.send(
            "当前运行在 Docker 中，但没有挂载仓库目录，#更新无法执行。\n"
            "请在 docker-compose.yml 增加：\n"
            "- ./:/repo\n"
            "然后重建一次镜像；之后即可在容器内更新。"
        )
        return
    await context.send("正在更新 FxBot，稍等。")
    restart_ready = False
    repo = (_docker_repo() if docker else None) or project_root()

    def prepare_restart() -> None:
        nonlocal restart_ready
        _save_restart_target(context.event, "FxBot 更新完成，已重启")
        _set_pending_restart()
        restart_ready = True

    def on_pull_success() -> None:
        # 依赖没变就立刻承诺重启：后面的日志读取、报告发送即使被取消，代码也已更新。
        if not _deps_changed(repo):
            prepare_restart()

    async def sync_dependencies() -> tuple[bool, str]:
        if docker:
            return await _sync_docker_dependencies()
        return await _sync_project_dependencies(repo)

    try:
        ok, output, logs = await _git_pull(on_pull_success)
        if not ok:
            logger.error("git pull 失败: {}", _truncate(output))
            await _send_report(context, ["FxBot 更新失败。", "git pull 返回错误，完整信息已记录到后台日志。"])
            return
        status = "没有新提交" if _already_up_to_date(output) else "更新完成"
        if not restart_ready:
            # 依赖指纹变化（或首次/上次同步失败）才跑 uv sync，纯代码更新直接重启。
            synced, detail = await sync_dependencies()
            if not synced:
                logger.error("依赖同步失败: {}", _truncate(detail))
                hint = "；Windows 下请先关闭 Bot 再手动执行 uv sync" if os.name == "nt" else ""
                await asyncio.wait_for(
                    _send_report(
                        context,
                        [
                            "FxBot 依赖同步失败，本次没有重启。",
                            f"仓库代码已拉取，但依赖还没就绪{hint}。处理后台日志里的错误后，再发一次 #更新 即可。",
                            *logs,
                        ],
                    ),
                    timeout=_REPORT_TIMEOUT_SECONDS,
                )
                return
            _write_deps_stamp(repo)
            prepare_restart()
        try:
            await asyncio.wait_for(_send_report(context, [f"FxBot {status}。", *logs]), timeout=_REPORT_TIMEOUT_SECONDS)
        except Exception:
            logger.opt(exception=True).warning("更新成功通知未完成，仍执行重启")
    finally:
        if restart_ready:
            _request_exit()


def install(application: Application, configuration: ConfigManager) -> None:
    """注册核心管理命令；不属于插件，拉黑/拉白只改目标命令的权限条目。"""
    atexit.register(reexec_if_pending)
    application.resource(_start_notifier(application), _noop)
    owner = Permission.for_event(superuser_check(configuration))

    async def blacklist(context: HandlerContext) -> None:
        await _moderate_command(application, context, remove=False)

    async def unblacklist(context: HandlerContext) -> None:
        await _moderate_command(application, context, remove=True)

    definitions = []
    for name, label, pattern, handler in (
        ("restart", "重启", r"^[#＃]重启$", _restart),
        ("shutdown", "关机", r"^[#＃]关机$", _shutdown),
        ("update", "更新并重启", r"^[#＃]更新$", _update),
        ("blacklist", "按命令拉黑", _BLACKLIST_PATTERN, blacklist),
        ("unblacklist", "按命令拉白", _UNBLACKLIST_PATTERN, unblacklist),
    ):
        matcher = on_regex(pattern, name=name, display_name=label, priority=1, block=True, permission=owner)
        matcher.handle()(handler)
        definitions.append(matcher.snapshot())
    bind_matchers(application, "system", tuple(definitions))
