"""B 站直播订阅与推送：业务声明不管理框架任务。"""

from . import commands
from .scheduler import CHECK_INTERVAL_SECONDS, LivePoller
from ...core.plugin import PluginMetadata, scheduled

__plugin_meta__ = PluginMetadata(
    name="B站直播推送",
    description="订阅 B 站直播间，在主播开播时推送通知。",
    usage="#B站直播订阅 房间号；#B站直播取消 房间号；#B站直播列表",
)

store = commands.store
poller = LivePoller(store)


@scheduled(interval=CHECK_INTERVAL_SECONDS, run_on_start=True, name="poll")
async def check_live(context):
    store.validate()
    await poller.check(context.notifications, initial=context.first_run)
