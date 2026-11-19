"""独立应用装配入口。"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass

from fxbot.utils.paths import plugins_dir
from fxbot.core.plugins import PluginContext, load_plugins, discover_plugins
from fxbot.core.application import Application

from .db import init_database, close_database
from .config import ConfigManager, get_manager
from .console import account, log_store, log_stream
from .chat.lifecycle import ChatLifecycle
from .core.scheduler import Scheduler
from .membership.gate import MembershipGate
from .runtime.browser import install as install_browser_renderer
from .membership.guard import membership_guard
from .membership.tasks import MembershipMaintenance
from .runtime.commands import install
from .runtime.settings import Settings
from .membership.service import membership_service
from .permission.helpers import upsert_plugin_defaults, upsert_command_defaults
from .permission.storage import get_storage
from .membership.commands import MembershipCommands
from .console.chat_channel import console_chat
from .platforms.qq.service import QQService
from .runtime.message_stats import MessageStats
from .runtime.notifications import NotificationService
from .runtime.system_commands import install as install_system_commands
from .permission.authorization import Authorizer
from .permission.message_policy import MessagePolicy
from .runtime.plugin_dependencies import ensure_plugin_dependencies


@dataclass(frozen=True, slots=True)
class Runtime:
    """装配根显式保存应用及业务服务，不通过宿主查找。"""

    application: Application
    configuration: ConfigManager
    membership: MembershipMaintenance
    scheduler: Scheduler
    authorizer: Authorizer
    chat: ChatLifecycle
    qq: QQService
    message_stats: MessageStats


_dependencies_checked = False


def create_runtime(settings: Settings) -> Runtime:
    """基础命令和目录发现的插件使用同一注册流程。"""
    global _dependencies_checked
    plugins = discover_plugins()
    if not _dependencies_checked:
        # 同一进程只检查一次；只补缺失，不升级已装版本。冲突记录后继续装配。
        ensure_plugin_dependencies((definition.name for definition in plugins), plugins_dir())
        _dependencies_checked = True
    application = Application()
    message_stats = MessageStats()
    application.add_send_observer(message_stats.record_send)
    config = get_manager()
    scheduler = Scheduler(application)
    authorizer = Authorizer(config, get_storage())
    notifications = NotificationService(application, config, authorizer, membership_guard)
    maintenance = MembershipMaintenance(application, config, membership_service, membership_guard, scheduler)
    chat = ChatLifecycle(application, config, scheduler)
    resources = AsyncExitStack()

    async def start_services() -> None:
        """配置初始化先于数据库连接和业务调度。"""
        config.bootstrap()
        upsert_plugin_defaults("runtime", level="member", scene="all")
        upsert_command_defaults("runtime", "ping", level="member", scene="all")
        upsert_plugin_defaults("chat", level="member", scene="all")
        get_storage().flush()
        log_store.start()
        resources.callback(log_store.shutdown)
        log_stream.start()
        resources.callback(log_stream.stop)
        account.ensure_account()
        resources.push_async_callback(close_database)
        await init_database()
        maintenance.configure()
        resources.callback(config.subscribe(maintenance.configure))
        application.spawn(scheduler.run(), name="scheduler")

    application.resource(start_services, resources.aclose)
    application.resource(lambda: message_stats.start(application), message_stats.stop)
    application.router.add_preprocessor(MembershipGate(config, membership_guard))
    application.router.add_preprocessor(MessagePolicy(config))
    MembershipCommands(application, config, membership_service, membership_guard, settings, maintenance).install()
    install_browser_renderer(application)
    install_system_commands(application, config)
    install(application, authorizer)
    load_plugins(PluginContext(application, scheduler, authorizer, notifications, config), plugins)
    chat.install(authorizer)

    async def start_console() -> None:
        console_chat.start(application)

    application.resource(start_console, console_chat.stop)
    qq = QQService(application, settings)
    application.resource(qq.start, qq.close)
    return Runtime(application, config, maintenance, scheduler, authorizer, chat, qq, message_stats)
