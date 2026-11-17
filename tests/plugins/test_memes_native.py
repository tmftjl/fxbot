"""表情包插件原生路由、触发词、保护与调用记录回归。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from unittest.mock import Mock, AsyncMock, patch

from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fxbot.db import base_models
from fxbot.core import Message, HandlerContext, MessageSegment
from fxbot.config import SYSTEM_DEFAULTS
from fxbot.core.plugins import LoadedPlugin, PluginContext, PluginDefinition, load_plugins
from fxbot.core.matching import MatchResult
from fxbot.plugins.memes import manager as manager_module, recorder, protection, native_matchers as native
from fxbot.core.scheduler import Scheduler
from fxbot.core.application import Application
from fxbot.permission.types import perm_entry_default
from fxbot.platforms.onebot11 import OneBot11Client, parse_event
from fxbot.plugins.memes.models import MemeGenerationRecord
from fxbot.plugins.memes.manager import MemeManager
from fxbot.plugins.memes.request import (
    MemeInfo,
    ParserArg,
    MemeArgsType,
    ParserOption,
    MemeParamsType,
    CommandShortcut,
)
from fxbot.plugins.memes.exception import MemeGeneratorException
from tests.migration.test_onebot11 import payload
from fxbot.permission.authorization import Authorizer
from fxbot.plugins.memes.session_models import SessionBot, SessionUser, SessionScene, SessionIdentity

NOW = datetime.now(timezone.utc)


def meme_info(
    key: str,
    keywords: list[str],
    *,
    min_images: int = 0,
    max_images: int = 0,
    min_texts: int = 0,
    max_texts: int = 0,
    default_texts: list[str] | None = None,
    shortcuts: list[CommandShortcut] | None = None,
    tags: set[str] | None = None,
    args_type: MemeArgsType | None = None,
) -> MemeInfo:
    return MemeInfo(
        key=key,
        params_type=MemeParamsType(
            min_images=min_images,
            max_images=max_images,
            min_texts=min_texts,
            max_texts=max_texts,
            default_texts=default_texts or [],
            args_type=args_type,
        ),
        keywords=keywords,
        shortcuts=shortcuts or [],
        tags=tags or set(),
        date_created=NOW - timedelta(days=40),
        date_modified=NOW,
    )


FAKE_MEMES = {
    "jump": meme_info("jump", ["跳", "jump"], min_texts=1, max_texts=1, tags={"text"}),
    "petpet": meme_info(
        "petpet",
        ["摸", "摸头"],
        min_images=1,
        max_images=1,
        shortcuts=[CommandShortcut("pet", humanized="摸摸")],
        tags={"image"},
    ),
    "interaction": meme_info(
        "interaction",
        ["互动"],
        min_images=1,
        max_images=2,
        tags={"image"},
        args_type=MemeArgsType(
            args_model={},
            args_examples=[],
            parser_options=[ParserOption(names=["-t", "--text"], args=[ParserArg("value", "str")], help_text="文字")],
        ),
    ),
}


class _MemoryPermissionStorage:
    """不落盘的权限存储替身。"""

    def __init__(self) -> None:
        self.data = {"top": perm_entry_default(), "sub_plugins": {}}

    def load(self):
        return self.data

    def mark_dirty(self, data):
        self.data = data

    def flush(self):
        return None


class MemeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_metadata_never_blocks_chat_or_initializes_in_matcher(self):
        self.manager._ready = False
        chat = AsyncMock()
        self.app.router.message("test-chat", chat, priority=20, block=False)
        with patch.object(self.manager, "ensure_ready", AsyncMock(return_value=False)) as initialize:
            result = await self.dispatch([MessageSegment.text("普通聊天")])
            self.assertIn("test-chat", result.handled)
            self.assertFalse(self.manager._initialization_requested)
            result = await self.dispatch([MessageSegment.text("#jump hello")])
            self.assertIn("test-chat", result.handled)
            self.assertTrue(self.manager._initialization_requested)
            initialize.assert_not_awaited()
            await native._initialize_metadata(None)
            initialize.assert_awaited_once()
            self.assertFalse(self.manager._initialization_requested)
            await native._initialize_metadata(None)
            initialize.assert_awaited_once()
        self.generate.assert_not_awaited()
        self.assertEqual(chat.await_count, 2)

    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()

        self.manager = MemeManager(self.root / "memes_config.yaml")
        with (
            patch.object(manager_module, "get_meme_keys", AsyncMock(return_value=list(FAKE_MEMES))),
            patch.object(manager_module, "get_meme_info", AsyncMock(side_effect=lambda key: FAKE_MEMES[key])),
            patch.object(manager_module, "cfg_disabled_list", return_value=[]),
        ):
            await self.manager.init()

        self.patch_attr(native, "meme_manager", new=self.manager)
        self.patch_attr(native, "_trigger_revision", new=-1)
        self.patch_attr(native, "_trigger_manager", new=None)
        self.patch_attr(native, "cfg_command_prefixes", return_value=None)
        self.patch_attr(native, "cfg_notice_prob", return_value=0.0)
        self.patch_attr(native, "cfg_random_meme_show_info", return_value=False)
        self.patch_attr(native, "meme_list_cache_dir", return_value=self.cache)
        self.generate = self.patch_attr(native, "generate_meme", new_callable=AsyncMock, return_value=b"png")
        self.record = self.patch_attr(native, "record_meme_generation", new_callable=AsyncMock)

        self.system = deepcopy(SYSTEM_DEFAULTS)
        self.system["system"]["superusers"] = []
        self.config = Mock()
        self.config.get_system.side_effect = lambda: self.system
        self.storage = _MemoryPermissionStorage()
        self.auth = Authorizer(self.config, self.storage)

        self.app = Application()
        self.scheduler = Scheduler(self.app)
        load_plugins(
            PluginContext(self.app, self.scheduler, self.auth, Mock(), self.auth.configuration),
            (PluginDefinition("memes", "fxbot.plugins.memes"),),
        )
        await self.app.start()
        self.addAsyncCleanup(self.app.stop)

        self.calls: list[tuple[str, dict]] = []

        async def send(action, params):
            self.calls.append((action, params))
            return {"message_id": len(self.calls)}

        self.bot = OneBot11Client("100", send)
        self.app.register(self.bot, close=AsyncMock())

    def patch_attr(self, target, attribute, **kwargs):
        patcher = patch.object(target, attribute, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def event(self, segments: list[MessageSegment], *, role: str = "member", kind: str = "group"):
        data = payload(kind)
        data["sender"]["role"] = role
        data["message"] = []
        parsed = parse_event(data, bot_id="100")
        return replace(parsed, message=Message(segments), original_message=None)

    async def dispatch(self, segments: list[MessageSegment], *, role: str = "member", kind: str = "group"):
        return await self.app.router.dispatch(self.bot, self.event(segments, role=role, kind=kind))

    def sent_text(self) -> str:
        self.assertTrue(self.calls)
        message = self.calls[-1][1]["message"]
        return "".join(segment["data"].get("text", "") for segment in message if segment["type"] == "text")

    async def test_permission_defaults_and_priority_are_preserved(self) -> None:
        commands = self.storage.data["sub_plugins"]["memes"]["commands"]
        expected = {
            "list": "member",
            "detail": "member",
            "search": "member",
            "add_whitelist": "superuser",
            "remove_whitelist": "superuser",
            "protect_meme": "superuser",
            "unprotect_meme": "superuser",
            "protection_list": "superuser",
            "statistics": "member",
            "global_statistics": "member",
            "block": "member",
            "unblock": "member",
            "global_block": "superuser",
            "global_unblock": "superuser",
            "global_block_list": "superuser",
            "random": "member",
            "refresh": "superuser",
            "make": "member",
        }
        self.assertEqual({name: entry["level"] for name, entry in commands.items()}, expected)
        self.assertTrue(all(entry["scene"] == "all" for entry in commands.values()))

        definition = LoadedPlugin.read(PluginDefinition("memes", "fxbot.plugins.memes"))
        matchers = {item.name: item for item in definition.matchers}
        self.assertEqual((matchers["make"].priority, matchers["make"].block), (3, False))
        self.assertEqual((matchers["random"].priority, matchers["random"].block), (3, True))
        self.assertEqual((matchers["refresh"].priority, matchers["refresh"].block), (3, True))
        self.assertEqual((matchers["list"].priority, matchers["list"].block), (11, True))
        self.assertEqual((matchers["global_block"].priority, matchers["global_block"].block), (11, True))

        event = self.event([MessageSegment.text("#表情列表")])
        self.assertIsNotNone(matchers["list"].matches(event))
        self.assertIsNotNone(matchers["list"].matches(self.event([MessageSegment.text("#表情包制作")])))
        self.assertIsNotNone(matchers["list"].matches(self.event([MessageSegment.text("#头像表情包")])))
        self.assertIsNone(matchers["list"].matches(self.event([MessageSegment.text("#表情搜索 摸")])))

    async def test_member_command_routes_and_permission_denial(self) -> None:
        result = await self.dispatch([MessageSegment.text("#表情搜索 摸")])
        self.assertIn("memes:search", result.handled)
        self.assertIn("找到 1 个相关表情", self.sent_text())

        self.calls.clear()
        result = await self.dispatch([MessageSegment.text("#全局禁用表情 petpet")])
        self.assertIn("memes:global_block", result.denied)
        self.assertNotIn("memes:global_block", result.handled)
        self.assertEqual(self.calls, [])

    async def test_superuser_protection_command_uses_mention_argument(self) -> None:
        self.system["system"]["superusers"] = ["onebot11:100:private:200"]
        with (
            patch.object(protection, "cfg_whitelist_ids", return_value=[]),
            patch.object(protection, "cfg_protected_memes", return_value=[]),
            patch.object(protection, "save_protection_config") as save,
        ):
            result = await self.dispatch(
                [MessageSegment.text("#添加保护 "), MessageSegment.mention("400")], role="member"
            )
        self.assertIn("memes:add_whitelist", result.handled)
        save.assert_called_once_with(["400"], [])
        self.assertIn("已将 400 添加到保护白名单", self.sent_text())

    async def test_list_command_renders_cached_image_without_network(self) -> None:
        self.patch_attr(native, "get_meme_generation_keys", new_callable=AsyncMock, return_value=[])
        render = self.patch_attr(native, "render_meme_list", new_callable=AsyncMock, return_value=b"jpg")
        result = await self.dispatch([MessageSegment.text("#表情列表")])
        self.assertIn("memes:list", result.handled)
        render.assert_awaited_once()
        self.assertEqual([segment["type"] for segment in self.calls[-1][1]["message"]], ["text", "image"])
        self.assertIn("目前支持的表情列表", self.sent_text())

    async def test_group_admin_can_block_and_member_is_rejected_by_handler(self) -> None:
        result = await self.dispatch([MessageSegment.text("#禁用表情 petpet")], role="admin")
        self.assertIn("memes:block", result.handled)
        self.assertIn("禁用成功", self.sent_text())
        self.assertFalse(self.manager.check(native.get_user_id(self.event([])), "petpet"))

        self.calls.clear()
        result = await self.dispatch([MessageSegment.text("#禁用表情 jump")], role="member")
        self.assertIn("memes:block", result.handled)
        self.assertIn("权限不足", self.sent_text())

    async def test_trigger_rule_prefixes_shortcuts_and_parameter_message(self) -> None:
        event = self.event([MessageSegment.text("#jump 你好")])
        context = HandlerContext(self.bot, event, MatchResult(event.message.plain_text().strip()))
        self.assertTrue(await native.meme_trigger_rule(context))
        self.assertEqual(context.state[native.MEME_TRIGGER_KEY], "jump")
        self.assertEqual(context.state[native.MEME_MSG_KEY].plain_text(), "你好")

        event = self.event([MessageSegment.text("#JUMP 你好")])
        context = HandlerContext(self.bot, event, MatchResult(event.message.plain_text().strip()))
        self.assertTrue(await native.meme_trigger_rule(context))

        event = self.event([MessageSegment.text("#摸摸")])
        context = HandlerContext(self.bot, event, MatchResult(event.message.plain_text().strip()))
        self.assertTrue(await native.meme_trigger_rule(context))
        self.assertEqual(context.state[native.MEME_TRIGGER_KEY], "摸摸")

        event = self.event([MessageSegment.text("#不存在 你好")])
        context = HandlerContext(self.bot, event, MatchResult(event.message.plain_text().strip()))
        self.assertFalse(await native.meme_trigger_rule(context))

        with patch.object(native, "cfg_command_prefixes", return_value=["!"]):
            self.assertFalse(
                await native.meme_trigger_rule(
                    HandlerContext(self.bot, self.event([MessageSegment.text("#jump 你好")]), MatchResult(""))
                )
            )
            self.assertTrue(
                await native.meme_trigger_rule(
                    HandlerContext(self.bot, self.event([MessageSegment.text("!jump 你好")]), MatchResult(""))
                )
            )

    async def test_trigger_command_precedence_and_parameter_mismatch(self) -> None:
        event = self.event([MessageSegment.text("#随机表情")])
        context = HandlerContext(self.bot, event, MatchResult(event.message.plain_text().strip()))
        self.assertFalse(await native.meme_trigger_rule(context))

        result = await self.dispatch([MessageSegment.text("#jump 一 二")])
        self.assertIn("memes:make", result.handled)
        self.assertIn("文字数量不符，文字数量应为 1", self.sent_text())
        self.generate.assert_not_awaited()
        self.record.assert_not_awaited()

        self.patch_attr(native, "_sender_avatar_url", return_value=None)
        self.calls.clear()
        result = await self.dispatch([MessageSegment.text("#摸")])
        self.assertIn("memes:make", result.handled)
        self.assertIn("图片数量不符，图片数量应为 1", self.sent_text())
        self.generate.assert_not_awaited()

    async def test_successful_trigger_send_is_text_and_image(self) -> None:
        result = await self.dispatch([MessageSegment.text("#jump 你好")])
        self.assertIn("memes:make", result.handled)
        self.generate.assert_awaited_once_with("jump", [], ["你好"], args={})
        self.record.assert_awaited_once()
        self.assertEqual(self.calls[-1][0], "send_group_msg")
        self.assertEqual([segment["type"] for segment in self.calls[-1][1]["message"]], ["image"])

    async def test_generator_failure_reports_message_without_record(self) -> None:
        self.generate.side_effect = MemeGeneratorException("生成失败")
        result = await self.dispatch([MessageSegment.text("#jump 你好")])
        self.assertIn("memes:make", result.handled)
        self.assertEqual(self.sent_text(), "生成失败")
        self.record.assert_not_awaited()
        self.assertEqual([segment["type"] for segment in self.calls[-1][1]["message"]], ["text"])

    async def test_missing_meme_reports_not_found(self) -> None:
        result = await self.dispatch([MessageSegment.text("#表情详情 不存在")])
        self.assertIn("memes:detail", result.handled)
        self.assertIn("表情 不存在 不存在！", self.sent_text())

    async def test_parser_option_display_uses_standard_library(self) -> None:
        option = ParserOption(
            names=["--text", "-t"],
            args=[ParserArg("value", "str"), ParserArg("count", "int", default=1)],
            help_text="文本",
        )
        self.assertEqual(option.display(), "value -t│--text=文本")


class ProtectionTests(unittest.IsolatedAsyncioTestCase):
    def test_add_remove_and_duplicate_protection_entries(self) -> None:
        whitelist: list[str] = []
        protected: list[str] = []

        def save(users, memes):
            whitelist[:] = users
            protected[:] = memes

        with (
            patch.object(protection, "cfg_whitelist_ids", side_effect=lambda: list(whitelist)),
            patch.object(protection, "cfg_protected_memes", side_effect=lambda: list(protected)),
            patch.object(protection, "save_protection_config", side_effect=save),
        ):
            manager = protection.ProtectionManager()
            self.assertTrue(manager.add_whitelist("200"))
            self.assertFalse(manager.add_whitelist("200"))
            self.assertTrue(manager.remove_whitelist("200"))
            self.assertFalse(manager.remove_whitelist("200"))
            self.assertTrue(manager.add_protected_meme("petpet"))
            self.assertFalse(manager.add_protected_meme("petpet"))
            self.assertTrue(manager.remove_protected_meme("petpet"))
            self.assertFalse(manager.remove_protected_meme("petpet"))

    def test_superuser_identity_and_legacy_raw_id_are_protected(self) -> None:
        system = {"system": {"superusers": ["onebot11:100:private:200", "300"]}}
        configuration = Mock()
        configuration.get_system.return_value = system
        with (
            patch.object(protection, "cfg_whitelist_ids", return_value=[]),
            patch.object(protection, "get_manager", return_value=configuration),
        ):
            manager = protection.ProtectionManager()
            self.assertTrue(manager.is_in_whitelist("200", platform="onebot11", bot_id="100"))
            self.assertTrue(manager.is_in_whitelist("300", platform="onebot11", bot_id="100"))
            self.assertFalse(manager.is_in_whitelist("400", platform="onebot11", bot_id="100"))
            self.assertFalse(manager.is_in_whitelist("400", platform="qq", bot_id="100"))

    def test_protected_mentions_are_reprioritized(self) -> None:
        meme = meme_info("interaction", ["互动"], min_images=1, max_images=2)
        images = [b"a", b"b"]
        ids = ["200", "400"]
        mentions = [True, True]
        configuration = Mock()
        configuration.get_system.return_value = {"system": {"superusers": []}}
        with (
            patch.object(protection, "cfg_whitelist_ids", return_value=["200"]),
            patch.object(protection, "cfg_protected_memes", return_value=["interaction"]),
            patch.object(protection, "get_manager", return_value=configuration),
        ):
            ordered, ordered_ids, _ = native._reprioritize_protected_mentions(
                meme, list(images), list(ids), list(mentions), True, native_event()
            )
            self.assertEqual(ordered, [b"a", b"b"])
            self.assertEqual(ordered_ids, ["200", "400"])

            single = meme_info("interaction", ["互动"], min_images=1, max_images=1)
            ordered, ordered_ids, _ = native._reprioritize_protected_mentions(
                single, [b"a"], ["200"], [True], True, native_event()
            )
            self.assertEqual(ordered, [])
            self.assertEqual(ordered_ids, [])


def native_event():
    data = payload()
    data["sender"]["role"] = "member"
    data["message"] = []
    return parse_event(data, bot_id="100")


class MemeRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'test.db'}")
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patch_attr(base_models, "_db_initialized", True)
        self.patch_attr(base_models, "engine", self.engine)
        self.patch_attr(base_models, "async_maker", self.maker)
        self.patch_attr(base_models, "sqlite_semaphore", asyncio.Semaphore(20))

    def patch_attr(self, target, attribute, value):
        patcher = patch.object(target, attribute, value)
        self.addCleanup(patcher.stop)
        patcher.start()
        return value

    def event(self, *, role: str = "admin", kind: str = "group"):
        data = payload(kind)
        data["sender"]["role"] = role
        data["message"] = []
        return parse_event(data, bot_id="100")

    async def test_record_writes_legacy_session_tables_and_is_readable(self) -> None:
        event = self.event()
        await recorder.record_meme_generation(event, "jump")

        async with self.maker() as session:
            records = (await session.execute(select(MemeGenerationRecord))).scalars().all()
            bots = (await session.execute(select(SessionBot))).scalars().all()
            scenes = (await session.execute(select(SessionScene))).scalars().all()
            users = (await session.execute(select(SessionUser))).scalars().all()
            sessions = (await session.execute(select(SessionIdentity))).scalars().all()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].meme_key, "jump")
        self.assertEqual(records[0].session_persist_id, sessions[0].id)
        self.assertEqual((bots[0].self_id, bots[0].adapter, bots[0].scope), ("100", "OneBot V11", "Unknown"))
        self.assertEqual((scenes[0].scene_id, scenes[0].scene_type), ("300", 1))
        self.assertEqual(users[0].user_id, "200")
        self.assertEqual(sessions[0].member_data["roles"][0]["name"], "admin")

        start = datetime.now(timezone.utc) - timedelta(days=1)
        found = await recorder.get_meme_generation_records(event, recorder.SessionIdType.GROUP, time_start=start)
        self.assertEqual([(record.meme_key, record.time is not None) for record in found], [("jump", True)])
        self.assertEqual(
            await recorder.get_meme_generation_keys(event, recorder.SessionIdType.GLOBAL, time_start=start),
            ["jump"],
        )
        self.assertEqual(
            await recorder.get_meme_generation_records(
                self.event(kind="private"), recorder.SessionIdType.GROUP, time_start=start
            ),
            [],
        )

    async def test_existing_legacy_record_is_read_without_schema_change(self) -> None:
        event = self.event()
        async with self.maker() as session:
            bot = SessionBot(self_id="100", adapter="OneBot V11", scope="Unknown")
            session.add(bot)
            await session.flush()
            scene = SessionScene(
                bot_persist_id=bot.id,
                scene_id="300",
                scene_type=1,
                scene_data={"id": "300", "type": 1, "name": None, "avatar": None, "parent": None},
            )
            user = SessionUser(
                bot_persist_id=bot.id,
                user_id="200",
                user_data={"id": "200", "name": "旧用户", "nick": "旧用户", "avatar": None, "gender": "unknown"},
            )
            session.add(scene)
            session.add(user)
            await session.flush()
            legacy = SessionIdentity(
                bot_persist_id=bot.id,
                scene_persist_id=scene.id,
                user_persist_id=user.id,
                member_data=None,
            )
            session.add(legacy)
            await session.flush()
            session.add(
                MemeGenerationRecord(
                    session_persist_id=legacy.id,
                    time=datetime.now(timezone.utc),
                    meme_key="petpet",
                )
            )
            await session.commit()

        records = await recorder.get_meme_generation_records(event, recorder.SessionIdType.GROUP)
        self.assertEqual([record.meme_key for record in records], ["petpet"])
        times = await recorder.get_meme_generation_times(event, recorder.SessionIdType.USER)
        self.assertEqual(len(times), 1)


if __name__ == "__main__":
    unittest.main()
