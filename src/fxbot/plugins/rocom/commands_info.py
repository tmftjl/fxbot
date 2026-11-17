"""洛克王国资料查询命令。"""

from __future__ import annotations

from .data import (
    find_pet,
    can_breed,
    find_skill,
    search_eggs,
    search_pets,
    parse_search_criteria,
)
from .paths import POKEDEX_DIR
from ...core import Message, HandlerContext, MessageSegment
from ...core.plugin import on_regex
from .renderer_pokedex import render_pokedex_image
from .resource_downloader import ensure_rocom_resources

ATTRIBUTE_EFFECTIVENESS_IMAGE = POKEDEX_DIR / "attribute_effectiveness.png"

pokedex_query = on_regex(
    r"^[#＃]图鉴\s+(.+?)\s*$",
    name="rocom_pokedex_query",
    display_name="洛克图鉴",
    priority=5,
    block=True,
    level="member",
    scene="group",
)

skill_query = on_regex(
    r"^[#＃]技能信息\s+(.+?)\s*$",
    name="rocom_skill_query",
    display_name="洛克技能信息",
    priority=5,
    block=True,
    level="member",
    scene="group",
)

breed_query = on_regex(
    r"^[#＃]配种\s+(\S+)\s+(\S+)\s*$",
    name="rocom_breed_query",
    display_name="洛克配种查询",
    priority=5,
    block=True,
    level="member",
    scene="group",
)

egg_query = on_regex(
    r"^[#＃](?:精灵蛋|查蛋)\s+([0-9]+(?:\.[0-9]+)?)\s+([0-9]+(?:\.[0-9]+)?)(?:\s+(炫彩|同乘))?\s*$",
    name="rocom_egg_query",
    display_name="洛克精灵蛋查询",
    priority=5,
    block=True,
    level="member",
    scene="group",
)

pet_search = on_regex(
    r"^[#＃]查找精灵\s+(.+?)\s*$",
    name="rocom_pet_search",
    display_name="洛克查找精灵",
    priority=5,
    block=True,
    level="member",
    scene="group",
)

attribute_effectiveness = on_regex(
    r"^[#＃]属性克制\s*$",
    name="rocom_attribute_effectiveness",
    display_name="洛克属性克制",
    priority=5,
    block=True,
    level="member",
    scene="group",
)

resource_download = on_regex(
    r"^[#＃]洛克下载资源\s*$",
    name="rocom_resource_download",
    display_name="洛克资源下载",
    priority=5,
    block=True,
    level="owner",
    scene="group",
)


@pokedex_query.handle()
async def _handle_pokedex(context: HandlerContext) -> None:
    """查询精灵图鉴。"""
    name = context.match.group(1).strip()
    result = find_pet(name)
    if not result:
        await context.finish(f"未找到精灵：{name}")
    pet_id, pet = result
    image = await render_pokedex_image(pet, pet_id)
    await context.finish(Message([MessageSegment.image(image)]))


@skill_query.handle()
async def _handle_skill(context: HandlerContext) -> None:
    """查询技能信息。"""
    name = context.match.group(1).strip()
    skill = find_skill(name)
    if not skill:
        await context.finish(f"未找到技能：{name}")
    lines = [
        f"技能名称：{skill.get('name') or name}",
        f"属性：{skill.get('families') or '无'}",
        f"消耗：{skill.get('cost') or '0'}",
        f"威力：{skill.get('power') or '0'}",
        f"介绍：{skill.get('desc') or '暂无'}",
    ]
    await context.finish("\n".join(lines))


@breed_query.handle()
async def _handle_breed(context: HandlerContext) -> None:
    """查询配种兼容性。"""
    mother_name = context.match.group(1).strip()
    father_name = context.match.group(2).strip()
    mother = find_pet(mother_name)
    father = find_pet(father_name)
    if not mother:
        await context.finish(f"未找到母精灵：{mother_name}")
    if not father:
        await context.finish(f"未找到父精灵：{father_name}")
    _, mother_pet = mother
    _, father_pet = father
    ok, shared = can_breed(mother_pet, father_pet)
    if ok:
        await context.finish(f"{mother_pet['name']} 与 {father_pet['name']} 可以配种\n共同蛋组：{'、'.join(shared)}")
    await context.finish(f"{mother_pet['name']} 与 {father_pet['name']} 不能配种")


@egg_query.handle()
async def _handle_egg(context: HandlerContext) -> None:
    """按蛋尺寸和重量反查精灵。"""
    length_m = float(context.match.group(1))
    weight_kg = float(context.match.group(2))
    egg_type = context.match.group(3).strip()
    results = search_eggs(length_m, weight_kg, egg_type)
    if not results:
        await context.finish("没有找到符合条件的精灵蛋")
    label = f"{egg_type}精灵蛋" if egg_type else "精灵蛋"
    shown = "、".join(results[:40])
    suffix = f"\n另有 {len(results) - 40} 个结果未展示" if len(results) > 40 else ""
    await context.finish(f"可能的{label}：\n{shown}{suffix}")


@pet_search.handle()
async def _handle_pet_search(context: HandlerContext) -> None:
    """按条件查找精灵。"""
    raw = context.match.group(1).strip()
    criteria = parse_search_criteria(raw)
    if not criteria:
        await context.finish("请输入查询条件，例如：#查找精灵 属性:火 速度:>100")
    results = search_pets(criteria)
    if not results:
        await context.finish("没有找到符合条件的精灵")
    names = [str(pet.get("name") or pet_id) + (str(pet.get("form") or "")) for pet_id, pet in results]
    shown = "、".join(names[:60])
    suffix = f"\n仅展示前 60 个，共匹配 {len(results)} 个" if len(results) > 60 else f"\n共匹配 {len(results)} 个"
    await context.finish(f"查找结果：\n{shown}{suffix}")


@attribute_effectiveness.handle()
async def _handle_attribute_effectiveness(context: HandlerContext) -> None:
    """发送属性克制表。"""
    if not ATTRIBUTE_EFFECTIVENESS_IMAGE.exists():
        await context.finish("属性克制表资源不存在，请检查插件资源文件")
    image = ATTRIBUTE_EFFECTIVENESS_IMAGE.read_bytes()
    await context.finish(Message([MessageSegment.image(image)]))


@resource_download.handle()
async def _handle_resource_download(context: HandlerContext) -> None:
    """手动下载洛克王国运行时资源。"""
    await context.send("开始检查洛克王国资源，首次下载可能需要较久")
    try:
        await ensure_rocom_resources(force=True)
    except Exception as exc:
        await context.finish(f"洛克王国资源下载失败：{exc}")
    await context.finish("洛克王国资源检查完成")
