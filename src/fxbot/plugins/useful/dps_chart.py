"""鸣潮 DPS 榜图片命令。"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from .media import image_sources, fetch_image_bytes
from ...core import Message, HandlerContext, MessageSegment
from ...core.plugin import on_regex
from ...utils.paths import data_dir

DPS_IMAGE_PATH = data_dir("useful") / "wwdps.png"

wwdps_cmd = on_regex(
    r"^wwdps$",
    name="wwdps",
    display_name="鸣潮DPS榜",
    priority=5,
    block=True,
    level="all",
    scene="all",
)

update_wwdps_cmd = on_regex(
    r"^ww更新dps\s*(.*)$",
    name="update_wwdps",
    display_name="更新鸣潮DPS榜",
    priority=5,
    block=True,
    level="all",
    scene="all",
)


def _is_valid_image(image_bytes: bytes) -> bool:
    """校验字节内容是否为可识别图片。"""
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.verify()
        return True
    except Exception:
        return False


@wwdps_cmd.handle()
async def _handle_wwdps(context: HandlerContext) -> None:
    """发送当前鸣潮 DPS 榜图片。"""
    if not DPS_IMAGE_PATH.is_file():
        await context.finish("DPS榜图片尚未设置，请发送图片并使用 ww更新dps 更新。")
    await context.finish(Message([MessageSegment.image(DPS_IMAGE_PATH.read_bytes())]))


@update_wwdps_cmd.handle()
async def _handle_update_wwdps(context: HandlerContext) -> None:
    """更新鸣潮 DPS 榜图片。"""
    sources = await image_sources(context.bot, context.event)
    if not sources:
        await context.finish("未找到图片，请发送带图消息或回复/引用带图消息后使用 ww更新dps")

    image_bytes = await fetch_image_bytes(sources[0])
    if not image_bytes or not _is_valid_image(image_bytes):
        await context.finish("图片读取失败，请重新发送图片后再试")

    tmp_path = DPS_IMAGE_PATH.with_suffix(".tmp")
    tmp_path.write_bytes(image_bytes)
    tmp_path.replace(DPS_IMAGE_PATH)
    await context.finish("DPS榜图片已更新")
