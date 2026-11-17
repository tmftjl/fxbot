"""洛克王国原生迁移：路由权限、状态文件、纯函数、计划与推送。"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock, patch

from fxbot.core import SessionTarget
from fxbot.config import SYSTEM_DEFAULTS, ConfigManager
from fxbot.utils.tz import SHANGHAI_TZ
from fxbot.core.plugins import LoadedPlugin, PluginContext, PluginDefinition, load_plugins
from fxbot.plugins.rocom import data, config, scheduler as rocom_scheduler, commands_info, commands_merchant
from fxbot.core.scheduler import Daily, Scheduler
from fxbot.core.application import Application
from fxbot.permission.types import perm_entry_default
from fxbot.permission.storage import PermissionStorage
from fxbot.platforms.onebot11 import OneBot11Client, parse_event
from fxbot.plugins.rocom.store import RocomStore, subscription_target
from fxbot.plugins.rocom.client import MerchantProduct, MerchantSnapshot, parse_merchant_json
from tests.migration.test_onebot11 import payload
from fxbot.permission.authorization import Authorizer

GROUP = SessionTarget("onebot11", "100", "group", "300")
PRIVATE = SessionTarget("onebot11", "100", "private", "200")


def merchant_snapshot(signature: str = "sig-1") -> MerchantSnapshot:
    return MerchantSnapshot(
        source_url="https://example.test/merchant",
        title="洛克王国世界远行商人",
        round_no=1,
        updated_at="",
        next_refresh="12:00",
        remaining_time="1时0分",
        products=[MerchantProduct("测试商品", "", "08:00", "12:00", "测试商品 / 价格 1")],
        plain_text="{}",
        signature=signature,
    )


class StoreTests(unittest.TestCase):
    """state.json 保持旧字段，并能成对读写群与私聊订阅。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.store = RocomStore(self.path)

    def test_group_and_private_subscription_round_trip(self) -> None:
        group_target = {
            "user_id": "200",
            "session_id": GROUP.key(),
            "platform": "onebot11",
            "bot_id": "100",
            "group_id": "300",
        }
        private_target = {"user_id": "200", "session_id": PRIVATE.key(), "platform": "onebot11", "bot_id": "100"}
        self.store.upsert_subscription("group", "300", group_target, "200")
        self.store.upsert_subscription("private", "200", private_target, "200")
        self.store.set_last_signature("sig")

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw), {"last_signature", "subscriptions"})
        self.assertEqual(raw["last_signature"], "sig")
        self.assertEqual(len(raw["subscriptions"]), 2)
        self.assertEqual(set(raw["subscriptions"][0]), {"type", "group_key", "target", "operator_id"})
        self.assertEqual(raw["subscriptions"][0]["type"], "group")
        self.assertEqual(raw["subscriptions"][0]["group_key"], "300")
        self.assertEqual(raw["subscriptions"][0]["operator_id"], "200")
        self.assertEqual(raw["subscriptions"][1]["user_key"], "200")
        self.assertEqual(subscription_target(raw["subscriptions"][0]), GROUP)
        self.assertEqual(subscription_target(raw["subscriptions"][1]), PRIVATE)

        self.assertEqual(self.store.get_last_signature(), "sig")
        self.assertIsNotNone(self.store.get_subscription("group", "300"))
        self.assertTrue(self.store.remove_subscription("group", "300"))
        self.assertFalse(self.store.remove_subscription("group", "300"))
        self.assertIsNone(self.store.get_subscription("group", "300"))
        self.assertIsNotNone(self.store.get_subscription("private", "200"))

    def test_legacy_subscription_without_bot_identity_is_not_guessed(self) -> None:
        self.assertIsNone(subscription_target({"type": "group", "group_key": "300", "target": {"group_id": "300"}}))


class DataTests(unittest.TestCase):
    """配种、蛋反查和条件解析保持旧版纯函数语义。"""

    def test_can_breed_returns_shared_egg_groups(self) -> None:
        mother = {"egg_group": ["魔力猫", "动物组"]}
        father = {"egg_group": ["动物组", "不死组"]}
        self.assertEqual(data.can_breed(mother, father), (True, ["动物组"]))
        self.assertEqual(data.can_breed({"egg_group": ["A"]}, {"egg_group": ["B"]}), (False, []))

    def test_search_eggs_filters_size_weight_and_type(self) -> None:
        pets = {
            "1": {
                "name": "甲",
                "form": "",
                "breeding": {"height_low": 20, "height_high": 30, "weight_low": 100, "weight_high": 200},
                "talent_random_list": ["同乘"],
            },
            "2": {
                "name": "乙",
                "form": "形态",
                "breeding": {"height_low": 40, "height_high": 50, "weight_low": 300, "weight_high": 400},
            },
            "3": {
                "name": "丙",
                "form": "",
                "breeding": {"height_low": 20, "height_high": 30, "weight_low": 100, "weight_high": 200},
            },
        }
        with patch.object(data, "get_pet_list", return_value=pets):
            self.assertEqual(data.search_eggs(0.23, 0.15), ["甲", "丙"])
            self.assertEqual(data.search_eggs(0.45, 0.35), ["乙形态"])
            self.assertEqual(data.search_eggs(0.23, 0.15, "同乘"), ["甲"])
            self.assertEqual(data.search_eggs(0.23, 0.15, "炫彩"), ["甲"])

    def test_parse_search_criteria(self) -> None:
        self.assertEqual(
            data.parse_search_criteria("属性:火 速度:>100"),
            {"属性": "火", "速度": ">100"},
        )
        self.assertEqual(data.parse_search_criteria("喵喵"), {"名字": "喵喵"})
        self.assertTrue(data._compare_number(101, ">100"))
        self.assertFalse(data._compare_number(100, ">100"))
        self.assertTrue(data._compare_number(100, ">=100"))

    def test_parse_merchant_json_is_stable(self) -> None:
        raw = {
            "status": "open",
            "round": 2,
            "items": [{"name": "炫彩精灵蛋", "priceRaw": "100", "limit": "1", "image": "icon.png"}],
            "startedAtBeijing": "12:00",
            "nextRefreshBeijing": "16:00",
        }
        first = parse_merchant_json("source", raw)
        second = parse_merchant_json("source", raw)
        self.assertEqual(first.round_no, 2)
        self.assertEqual(first.products[0].detail, "炫彩精灵蛋 / 价格 100 / 限购 1")
        self.assertEqual(first.products[0].starttime, "12:00")
        self.assertEqual(first.signature, second.signature)


class ConfigAndPlanTests(unittest.TestCase):
    """计划默认保持 08/12/16/20 点 05 分，关闭后移除任务。"""

    def test_default_plan_matches_legacy_cron(self) -> None:
        settings = deepcopy(config.DEFAULTS)
        with patch.object(rocom_scheduler, "get_config", return_value=settings):
            plan = rocom_scheduler.merchant_plan(None)
        self.assertIsInstance(plan, Daily)
        self.assertEqual(plan.hours, (8, 12, 16, 20))
        self.assertEqual(plan.minute, 5)
        self.assertIs(plan.zone, SHANGHAI_TZ)
        now = datetime(2026, 1, 1, 0, 4, tzinfo=timezone.utc)
        self.assertEqual(plan.delay(now), 60)
        settings["merchant"]["push_hours"] = ["8", "20"]
        with patch.object(rocom_scheduler, "get_config", return_value=settings):
            plan = rocom_scheduler.merchant_plan(None)
        self.assertEqual(plan.hours, (8, 20))

    def test_disabled_merchant_removes_plan(self) -> None:
        settings = deepcopy(config.DEFAULTS)
        settings["merchant"]["enabled"] = False
        with patch.object(rocom_scheduler, "get_config", return_value=settings):
            self.assertIsNone(rocom_scheduler.merchant_plan(None))

    def test_config_validation_rejects_bad_values(self) -> None:
        for value in (
            {"merchant": {"enabled": "false"}},
            {"merchant": {"push_hours": []}},
            {"merchant": {"push_hours": [24]}},
            {"merchant": {"push_minute": 60}},
            {"resources": {"concurrency": 0}},
            {"resources": {"base_url": "ftp://example.test"}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.validate_config(value)
        config.validate_config(deepcopy(config.DEFAULTS))
        config.validate_config({"merchant": {"push_hours": ["8", "20"]}})


class PushTests(unittest.IsolatedAsyncioTestCase):
    """一次刷新只向当前订阅目标交付一次图片。"""

    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RocomStore(Path(directory.name) / "state.json")
        self.store.upsert_subscription(
            "group",
            "300",
            {"user_id": "200", "session_id": GROUP.key(), "platform": "onebot11", "bot_id": "100", "group_id": "300"},
            "200",
        )
        self.notifications = Mock()
        self.notifications.send = AsyncMock(return_value=True)
        self.render = patch.object(rocom_scheduler, "render_merchant_image", AsyncMock(return_value=b"PNG"))
        self.render.start()
        self.addCleanup(self.render.stop)

    async def test_successful_push_sends_image_and_records_signature(self) -> None:
        snapshot = merchant_snapshot()
        self.assertTrue(
            await rocom_scheduler.check_and_push(
                self.notifications,
                store=self.store,
                fetcher=AsyncMock(return_value=snapshot),
            )
        )
        call = self.notifications.send.await_args
        self.assertEqual(call.args[0], "rocom")
        self.assertEqual(call.args[1], GROUP)
        self.assertEqual([segment.type for segment in call.args[2].segments], ["image"])
        self.assertTrue(call.kwargs["valid"]())
        self.assertEqual(self.store.get_last_signature(), "sig-1")

        self.assertFalse(
            await rocom_scheduler.check_and_push(
                self.notifications,
                store=self.store,
                fetcher=AsyncMock(return_value=snapshot),
            )
        )
        self.assertEqual(self.notifications.send.await_count, 1)

    async def test_unavailable_target_does_not_hide_failed_delivery(self) -> None:
        self.notifications.send.return_value = False
        changed = await rocom_scheduler.check_and_push(
            self.notifications,
            store=self.store,
            fetcher=AsyncMock(return_value=merchant_snapshot("sig-2")),
        )
        self.assertTrue(changed)
        self.assertEqual(self.notifications.send.await_count, 1)
        self.assertEqual(self.store.get_last_signature(), "sig-2")

    async def test_no_products_is_not_pushed_or_recorded(self) -> None:
        snapshot = MerchantSnapshot("source", "title", 1, "", "", "", [], "{}", "empty")
        self.assertFalse(
            await rocom_scheduler.check_and_push(
                self.notifications,
                store=self.store,
                fetcher=AsyncMock(return_value=snapshot),
            )
        )
        self.notifications.send.assert_not_awaited()
        self.assertEqual(self.store.get_last_signature(), "")


class CommandTests(unittest.IsolatedAsyncioTestCase):
    """真实路由权限、订阅写入和下载失败语义。"""

    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RocomStore(Path(directory.name) / "state.json")
        self.settings = deepcopy(config.DEFAULTS)
        self.enterContext(patch.object(config, "get_config", side_effect=lambda: deepcopy(self.settings)))
        self.enterContext(patch.object(rocom_scheduler, "get_config", side_effect=lambda: deepcopy(self.settings)))
        self.enterContext(patch.object(commands_merchant, "state_store", self.store))
        self.enterContext(patch.object(rocom_scheduler, "state_store", self.store))
        self.fetch = self.enterContext(
            patch.object(
                rocom_scheduler,
                "fetch_merchant_snapshot",
                AsyncMock(return_value=merchant_snapshot("startup")),
            )
        )

        self.app = Application()
        self.scheduler = Scheduler(self.app)
        self.configuration = Mock(spec=ConfigManager)
        self.configuration.get_system.return_value = deepcopy(SYSTEM_DEFAULTS)
        self.permissions = {"top": perm_entry_default(), "sub_plugins": {}}
        self.storage = Mock(spec=PermissionStorage)
        self.storage.load.return_value = self.permissions
        self.authorizer = Authorizer(self.configuration, self.storage)
        load_plugins(
            PluginContext(
                self.app,
                self.scheduler,
                self.authorizer,
                Mock(),
                self.authorizer.configuration,
            ),
            (PluginDefinition("rocom", "fxbot.plugins.rocom"),),
        )
        await self.app.start()
        self.addAsyncCleanup(self.app.stop)
        self.bot = OneBot11Client("100", AsyncMock(return_value={"message_id": 1}))
        self.app.register(self.bot, close=AsyncMock())

    async def dispatch(self, text: str, *, role: str = "member", kind: str = "group"):
        incoming = payload(kind)
        incoming["message"] = [{"type": "text", "data": {"text": text}}]
        incoming["sender"]["role"] = role
        return await self.app.router.dispatch(self.bot, parse_event(incoming, bot_id="100"))

    def sent_texts(self) -> list[str]:
        texts = []
        for call in self.bot._sender.await_args_list:
            for segment in call.args[1]["message"]:
                if segment["type"] == "text":
                    texts.append(segment["data"]["text"])
        return texts

    async def test_config_save_replans_and_disabling_removes_job(self) -> None:
        job = next(item for item in self.scheduler.jobs if item.name == "rocom:merchant")
        self.assertEqual(job.schedule.hours, (8, 12, 16, 20))
        self.assertEqual(job.schedule.minute, 5)
        self.configuration.subscribe.assert_called_once()
        reconfigure = self.configuration.subscribe.call_args.args[0]
        self.settings["merchant"]["push_hours"] = [9]
        reconfigure()
        job = next(item for item in self.scheduler.jobs if item.name == "rocom:merchant")
        self.assertEqual(job.schedule.hours, (9,))
        self.settings["merchant"]["enabled"] = False
        reconfigure()
        self.assertNotIn("rocom:merchant", [item.name for item in self.scheduler.jobs])

    async def test_member_can_query_pokedex_and_gets_image(self) -> None:
        with (
            patch.object(commands_info, "find_pet", return_value=("1", {"name": "喵喵"})),
            patch.object(commands_info, "render_pokedex_image", AsyncMock(return_value=b"PNG")) as render,
        ):
            result = await self.dispatch("#图鉴 喵喵")
        self.assertEqual(result.failed, ())
        render.assert_awaited_once_with({"name": "喵喵"}, "1")
        message = self.bot._sender.await_args.args[1]["message"]
        self.assertEqual([segment["type"] for segment in message], ["image"])

    async def test_admin_subscribes_and_member_is_denied(self) -> None:
        denied = await self.dispatch("#开启远行商人", role="member")
        self.assertTrue(denied.denied)
        self.assertIsNone(self.store.get_subscription("group", "300"))

        result = await self.dispatch("#开启远行商人", role="admin")
        self.assertEqual(result.failed, ())
        subscription = self.store.get_subscription("group", "300")
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription["group_key"], "300")
        self.assertEqual(subscription["operator_id"], "200")
        self.assertEqual(subscription["target"]["platform"], "onebot11")
        self.assertEqual(subscription["target"]["bot_id"], "100")
        self.assertEqual(subscription["target"]["group_id"], "300")
        self.assertIn("已开启远行商人推送（本群）", self.sent_texts()[-1])

    async def test_member_can_check_private_subscription_status(self) -> None:
        result = await self.dispatch("#远行商人订阅", kind="private")
        self.assertEqual(result.failed, ())
        self.assertEqual(self.sent_texts()[-1], "该私聊没有远行商人推送订阅")

    async def test_download_command_is_owner_only(self) -> None:
        with patch.object(commands_info, "ensure_rocom_resources", AsyncMock()) as ensure:
            denied = await self.dispatch("#洛克下载资源", role="admin")
        self.assertTrue(denied.denied)
        ensure.assert_not_awaited()

        with patch.object(commands_info, "ensure_rocom_resources", AsyncMock()) as ensure:
            result = await self.dispatch("#洛克下载资源", role="owner")
        self.assertEqual(result.failed, ())
        ensure.assert_awaited_once_with(force=True)
        self.assertEqual(self.sent_texts()[-1], "洛克王国资源检查完成")

    async def test_download_failure_does_not_claim_success(self) -> None:
        with patch.object(
            commands_info,
            "ensure_rocom_resources",
            AsyncMock(side_effect=RuntimeError("没有找到可用的 GsCore 资源站")),
        ):
            result = await self.dispatch("#洛克下载资源", role="owner")
        self.assertEqual(result.failed, ())
        texts = self.sent_texts()
        self.assertIn("洛克王国资源下载失败：没有找到可用的 GsCore 资源站", texts)
        self.assertNotIn("洛克王国资源检查完成", texts)


class DefinitionTests(unittest.TestCase):
    """命令名、权限等级和场景与旧插件一致。"""

    def test_all_matchers_keep_legacy_levels_and_scenes(self) -> None:
        loaded = LoadedPlugin.read(PluginDefinition("rocom", "fxbot.plugins.rocom"))
        actual = {matcher.name: (matcher.level, matcher.scene) for matcher in loaded.matchers}
        self.assertEqual(
            actual,
            {
                "rocom_pokedex_query": ("member", "group"),
                "rocom_skill_query": ("member", "group"),
                "rocom_breed_query": ("member", "group"),
                "rocom_egg_query": ("member", "group"),
                "rocom_pet_search": ("member", "group"),
                "rocom_attribute_effectiveness": ("member", "group"),
                "rocom_resource_download": ("owner", "group"),
                "rocom_merchant_query": ("member", "all"),
                "rocom_merchant_subscribe": ("admin", "all"),
                "rocom_merchant_unsubscribe": ("admin", "all"),
                "rocom_merchant_subscription": ("member", "all"),
            },
        )
        self.assertEqual([declaration.name for _, declaration in loaded.schedules], ["merchant"])
        self.assertIn("console.config", loaded.extensions)
