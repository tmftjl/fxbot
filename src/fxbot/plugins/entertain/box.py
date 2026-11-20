"""QQ 公开资料卡，原生能力边界。"""

from __future__ import annotations

import re
import textwrap
from io import BytesIO
from typing import Any
from datetime import datetime

import httpx
from PIL import Image

from .config import cfg_box
from ...utils.tz import SHANGHAI_TZ
from ...core.types import Message, MessageSegment
from ...utils.http import get_shared_async_client
from ...core.plugin import on_event, on_regex
from ...core.capabilities import Capability


class Box:
    async def handle(self, context) -> None:
        cfg = cfg_box()
        event = context.event
        if Capability.QQ_PROFILE not in context.bot.capabilities:
            await context.finish("当前平台不支持 QQ 资料查询")
        if cfg["only_admin"]:
            if not event.is_group or await context.bot.capabilities.require(Capability.GROUP_ROLE).member_role(
                event.target.target_id, event.user_id
            ) not in {
                "owner",
                "admin",
            }:
                await context.finish("仅限群管理员可用")
        mentions = (event.original_message or event.message).mentions()
        number = re.search(r"\d{5,}", context.arguments)
        user_id = next((uid for uid in mentions if uid != event.bot_id), None) or (
            number.group() if number else event.user_id
        )
        if user_id in cfg["box_blacklist"]:
            await context.finish("该用户无法被开盒")
        await context.send(await self.render(context.bot, user_id, event.target.target_id if event.is_group else None))

    async def render(self, bot, user_id, group_id) -> Message:
        from .box_draw import create_image

        info = await bot.capabilities.require(Capability.QQ_PROFILE).get_user(user_id)
        member = (
            await bot.capabilities.require(Capability.MEMBER_LOOKUP).get_member(group_id, user_id) if group_id else {}
        )
        url = cfg_box()["avatar_api_url"].format(user_id=user_id)
        try:
            response = await (await get_shared_async_client()).get(url)
            response.raise_for_status()
            avatar = response.content
        except httpx.HTTPError:
            buffer = BytesIO()
            Image.new("RGB", (640, 640), "white").save(buffer, format="PNG")
            avatar = buffer.getvalue()
        return Message([MessageSegment.image(create_image(avatar, _transform_info(info, member)))])

    async def joined(self, context) -> None:
        cfg = cfg_box()
        event = context.event
        if (
            not cfg["increase_box"]
            or event.user_id == event.bot_id
            or Capability.QQ_PROFILE not in context.bot.capabilities
        ):
            return
        if event.target.target_id not in cfg["auto_box_groups"]:
            return
        if event.user_id not in cfg["box_blacklist"]:
            await context.send(await self.render(context.bot, event.user_id, event.target.target_id))


def _transform_info(info: dict[str, Any], member_info: dict[str, Any]) -> list[str]:
    """转换账号信息为展示文本。"""
    lines: list[str] = []
    if user_id := info.get("user_id"):
        lines.append(f"QQ号：{user_id}")
    if nickname := info.get("nickname"):
        lines.append(f"昵称：{nickname}")
    if card := member_info.get("card"):
        lines.append(f"群昵称：{card}")
    if title := member_info.get("title"):
        lines.append(f"头衔：{title}")
    sex = info.get("sex")
    if sex == "male":
        lines.append("性别：男")
    elif sex == "female":
        lines.append("性别：女")
    by, bm, bd = (
        info.get("birthday_year"),
        info.get("birthday_month"),
        info.get("birthday_day"),
    )
    if by and bm and bd:
        lines.append(f"诞辰：{by}-{bm}-{bd}")
        lines.append(f"星座：{_get_constellation(int(bm), int(bd))}")
        lines.append(f"生肖：{_get_zodiac(int(by), int(bm), int(bd))}")
    if age := info.get("age"):
        lines.append(f"年龄：{age}岁")
    for key, label in {
        "phoneNum": "电话",
        "eMail": "邮箱",
        "postCode": "邮编",
        "address": "地址",
        "remark": "备注",
        "labels": "标签",
    }.items():
        value = info.get(key)
        if value and value != "-":
            lines.append(f"{label}：{value}")
    country, province, city = (
        info.get("country"),
        info.get("province"),
        info.get("city"),
    )
    if country == "中国" and (province or city):
        lines.append(f"现居：{province or ''}-{city or ''}")
    elif country:
        lines.append(f"现居：{country}")
    if home_town := info.get("homeTown"):
        if home_town != "0-0-0":
            lines.append(f"来自：{_parse_home_town(str(home_town))}")
    if blood := info.get("kBloodType"):
        lines.append(f"血型：{_get_blood_type(int(blood))}")
    if career := info.get("makeFriendCareer"):
        if str(career) != "0":
            lines.append(f"职业：{_get_career(int(career))}")
    if info.get("is_vip"):
        lines.append("VIP：已开")
    if info.get("is_years_vip"):
        lines.append("年费 VIP：已开")
    if int(info.get("vip_level", 0) or 0) != 0:
        lines.append(f"VIP 等级：{info['vip_level']}")
    if int(info.get("login_days", 0) or 0) != 0:
        lines.append(f"连续登录天数：{info['login_days']}")
    if level := member_info.get("level"):
        lines.append(f"群等级：{int(level)}级")
    if join_time := member_info.get("join_time"):
        lines.append(f"加群时间：{datetime.fromtimestamp(int(join_time), SHANGHAI_TZ).strftime('%Y-%m-%d')}")
    if qq_level := info.get("qqLevel"):
        lines.append(f"QQ等级：{_qq_level_to_icon(int(qq_level))}")
    if reg_time := info.get("reg_time"):
        lines.append(f"注册时间：{datetime.fromtimestamp(int(reg_time), SHANGHAI_TZ).strftime('%Y-%m-%d')}")
    if long_nick := info.get("long_nick"):
        for line in textwrap.wrap(text=f"签名：{long_nick}", width=15):
            lines.append(line)
    return lines or [f"QQ号：{info.get('user_id', '')}"]


def _qq_level_to_icon(level: int) -> str:
    """转换 QQ 等级为图标文本。"""
    icons = ["👑", "☀", "🌙", "⭐"]
    levels = [64, 16, 4, 1]
    result = ""
    original = level
    for icon, item in zip(icons, levels):
        count, level = divmod(level, item)
        result += icon * count
    return f"{result}({original})"


def _get_constellation(month: int, day: int) -> str:
    """计算星座。"""
    items = {
        "白羊座": ((3, 21), (4, 19)),
        "金牛座": ((4, 20), (5, 20)),
        "双子座": ((5, 21), (6, 20)),
        "巨蟹座": ((6, 21), (7, 22)),
        "狮子座": ((7, 23), (8, 22)),
        "处女座": ((8, 23), (9, 22)),
        "天秤座": ((9, 23), (10, 22)),
        "天蝎座": ((10, 23), (11, 21)),
        "射手座": ((11, 22), (12, 21)),
        "摩羯座": ((12, 22), (1, 19)),
        "水瓶座": ((1, 20), (2, 18)),
        "双鱼座": ((2, 19), (3, 20)),
    }
    for name, ((start_month, start_day), (end_month, end_day)) in items.items():
        if (month == start_month and day >= start_day) or (month == end_month and day <= end_day):
            return name
        if start_month > end_month and (
            (month == start_month and day >= start_day) or (month == end_month and day <= end_day)
        ):
            return name
    return f"星座{month}-{day}"


def _get_zodiac(year: int, month: int, day: int) -> str:
    """计算生肖。"""
    zodiacs = ["龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪", "鼠", "牛", "虎", "兔"]
    zodiac_year = year - 1 if (month == 1) or (month == 2 and day < 4) else year
    return zodiacs[(zodiac_year - 2024) % 12]


def _get_career(num: int) -> str:
    """转换职业编码。"""
    careers = {
        1: "计算机/互联网/通信",
        2: "生产/工艺/制造",
        3: "医疗/护理/制药",
        4: "金融/银行/投资/保险",
        5: "商业/服务业/个体经营",
        6: "文化/广告/传媒",
        7: "娱乐/艺术/表演",
        8: "律师/法务",
        9: "教育/培训",
        10: "公务员/行政/事业单位",
        11: "模特",
        12: "空姐",
        13: "学生",
        14: "其他职业",
    }
    return careers.get(num, f"职业{num}")


def _get_blood_type(num: int) -> str:
    """转换血型编码。"""
    return {1: "A型", 2: "B型", 3: "O型", 4: "AB型", 5: "其他血型"}.get(num, f"血型{num}")


def _parse_home_town(code: str) -> str:
    """解析简化版家乡编码。"""
    country_map = {"49": "中国", "250": "俄罗斯", "222": "特立尼达", "217": "法国"}
    province_map = {
        "98": "北京",
        "99": "天津/辽宁",
        "100": "河北/山西",
        "101": "内蒙古/吉林",
        "102": "黑龙江/上海",
        "103": "江苏/浙江",
        "104": "安徽/福建",
        "105": "江西/山东",
        "106": "河南/湖北/湖南",
        "107": "新疆",
    }
    try:
        country_code, province_code, _ = code.split("-")
    except ValueError:
        return str(code)
    country = country_map.get(country_code, f"外国{country_code}")
    if country_code == "49" and province_code != "0":
        return province_map.get(province_code, f"{province_code}省")
    return country


show_box_matcher = on_regex(r"^(?:#|＃|/)(?:盒|开盒)\s*(.*)", name="box", display_name="QQ资料卡", level="all")


@show_box_matcher.handle()
async def show_box(context):
    await box.handle(context)


joined_matcher = on_event("member_joined", name="increase_box", display_name="入群抽盒", scene="group", priority=12)


@joined_matcher.handle()
async def joined(context):
    await box.joined(context)


box = Box()
