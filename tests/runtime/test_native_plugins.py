"""群管理、验证、娱乐与视频解析的原生协议回归；不访问外部服务。"""

from __future__ import annotations

import json
import time
import asyncio
import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock, AsyncMock, patch

from fxbot.core import Router, Message, SessionTarget, HandlerContext, MessageSegment, UnsupportedCapability
from fxbot.core.errors import ActionFailed
from fxbot.core.matching import MatchResult
from fxbot.utils.json_file import write_json
from fxbot.core.application import Application
from fxbot.platforms.onebot11 import OneBot11Client, parse_event
from fxbot.plugins.video_parser import matcher as video
from tests.migration.test_onebot11 import payload
from fxbot.plugins.entertain.config import validate_config as validate_entertain
from fxbot.plugins.entertain.napcat import Likes
from fxbot.plugins.entertain.welcome import Welcome
from fxbot.plugins.video_parser.types import VideoResult
from fxbot.plugins.group_admin.service import GroupAdminService
from fxbot.plugins.video_parser.config import validate_config as validate_video
from fxbot.plugins.video_parser.sender import send_image_result, send_video_result
from fxbot.plugins.entertain.musicshare import Song, Music
from fxbot.plugins.group_admin.banwords import Banwords
from fxbot.plugins.group_admin.commands import GroupCommands, parse_duration
from fxbot.platforms.onebot11.extensions import OneBot11ExtendedClient
from fxbot.plugins.entertain.image_display import ImageDisplay
from fxbot.plugins.group_admin.join_verify import Pending, JoinVerification
from fxbot.plugins.video_parser.parsers.base import ParseError, find_url, can_parse_url

GROUP = SessionTarget("onebot11", "100", "group", "300")


class NativePluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_mute_list_query_failures_reply_without_executing_unmute(self):
        commands = GroupCommands(self.service)
        for action, text in (("mute_list", "#禁言列表"), ("unmute_all", "#解除全部禁言")):
            for error, response in (
                (UnsupportedCapability("unknown"), "当前实现不支持禁言列表"),
                (ActionFailed("malformed"), "禁言列表查询失败，请稍后重试"),
                (TimeoutError(), "禁言列表查询失败，请稍后重试"),
            ):
                bot = OneBot11ExtendedClient("100", AsyncMock())
                bot.get_muted_members = AsyncMock(side_effect=error)
                bot.send = AsyncMock()
                event = replace(self.event, message=Message.text(text), original_message=None)
                with (
                    self.subTest(action=action, error=error),
                    patch.object(self.service, "execute", AsyncMock()) as execute,
                ):

                    async def handle(ctx):
                        await commands.handle(action, ctx)

                    router = Router()
                    router.message("query", handle)
                    result = await router.dispatch(bot, event)
                    self.assertEqual(result.failed, ())
                    bot.send.assert_awaited_once()
                    self.assertEqual(bot.send.await_args.args[1].plain_text(), response)
                    execute.assert_not_awaited()

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = Application()
        await self.app.start()
        self.addAsyncCleanup(self.app.stop)
        self.roles = {"100": "owner", "200": "owner", "400": "member"}
        self.calls = []

        async def send(action, params):
            self.calls.append((action, params))
            if action == "get_group_member_info":
                user = str(params["user_id"])
                return {"user_id": int(user), "role": self.roles.get(user)}
            return {"message_id": 1}

        self.bot = OneBot11Client("100", send)
        self.app.register(self.bot, close=AsyncMock())
        self.auth = Mock()
        self.auth.check = AsyncMock(return_value=True)
        self.auth.check_target = AsyncMock(return_value=True)
        self.auth.guard = lambda feature: self.auth.check.__call__
        self.auth.configuration.get_system.return_value = {"system": {"superusers": [], "bot_admins": []}}
        self.event = parse_event(payload(), bot_id="100")
        self.service = GroupAdminService(self.app, self.auth)

    def context(self, text="", **changes):
        event = replace(self.event, message=Message.text(text), original_message=None, **changes)
        return HandlerContext(self.bot, event, MatchResult(text))

    def mutations(self):
        return [(action, args) for action, args in self.calls if action.startswith(("set_", "delete_", "send_like"))]

    def verification(self):
        verifier = JoinVerification(self.service)
        verifier.path = self.root / "verify.json"
        write_json(verifier.path, {"300": {"enabled": True}})
        return verifier

    def pending(self, verifier, *, deadline=None):
        record = Pending(GROUP, self.bot, "200", "2345", 0, deadline or time.monotonic() + 60)
        verifier.pending[(GROUP.key(), "200")] = record
        return record

    def test_join_verify_configs_use_group_ids_and_skip_bad_entries(self):
        verifier = JoinVerification(self.service)
        verifier.path = self.root / "verify.json"
        write_json(
            verifier.path,
            {
                "300": {"enabled": True},
                "onebot11:100:group:301": {"enabled": True},
                "onebot11:100": {"enabled": True},
                "302": {"enabled": "yes"},
            },
        )
        self.assertEqual(verifier.configs(), {"300": {"enabled": True}})
        self.assertTrue(verifier.enabled(GROUP))
        self.assertEqual(json.loads(verifier.path.read_text(encoding="utf-8")), {"300": {"enabled": True}})

    async def test_moderation_exact_onebot_binding(self):
        result = await self.service.execute(self.bot, self.event, "mute_member", user_id="400", duration=60)
        self.assertTrue(result.success)
        self.assertEqual(self.mutations(), [("set_group_ban", {"group_id": 300, "user_id": 400, "duration": 60})])

    async def test_unknown_role_does_not_mutate(self):
        self.roles["400"] = None
        result = await self.service.execute(self.bot, self.event, "kick_member", user_id="400")
        self.assertFalse(result.success)
        self.assertEqual(self.mutations(), [])

    async def test_admin_cannot_kick_admin(self):
        self.roles.update({"100": "admin", "200": "admin", "400": "admin"})
        self.assertFalse((await self.service.execute(self.bot, self.event, "kick_member", user_id="400")).success)
        self.assertEqual(self.mutations(), [])

    async def test_owner_and_protected_target_are_not_kicked(self):
        for role, protected in (("owner", []), ("member", ["onebot11:100:private:400"])):
            self.roles["400"] = role
            self.auth.configuration.get_system.return_value["system"]["superusers"] = protected
            self.assertFalse((await self.service.execute(self.bot, self.event, "kick_member", user_id="400")).success)
        self.assertEqual(self.mutations(), [])

    async def test_permissions_revoked_during_lookup(self):
        self.auth.check.side_effect = [True, False]
        self.assertFalse((await self.service.execute(self.bot, self.event, "kick_member", user_id="400")).success)
        self.assertEqual(self.mutations(), [])

    async def test_stale_bot_not_replaced_with_new_connection(self):
        self.app.unregister(self.bot)
        self.assertFalse((await self.service.execute(self.bot, self.event, "kick_member", user_id="400")).success)
        self.assertEqual(self.calls, [])

    async def test_invalid_duration_no_lookup_or_mutation(self):
        for duration in (True, -1, 2592001):
            self.assertFalse(
                (
                    await self.service.execute(self.bot, self.event, "mute_member", user_id="400", duration=duration)
                ).success
            )
        self.assertEqual(self.calls, [])

    async def test_self_action_cannot_target_another_user(self):
        self.roles["200"] = "member"
        result = await self.service.execute(self.bot, self.event, "self_mute", user_id="400", duration=60)
        self.assertTrue(result.success)
        self.assertEqual(self.mutations()[0][1]["user_id"], 200)

    async def test_automatic_rechecks_permission_after_role_lookup(self):
        self.auth.check_target.side_effect = [True, False]
        self.assertFalse(await self.service.automatic_kick(self.bot, GROUP, "400"))
        self.assertEqual(self.mutations(), [])

    async def test_native_command_route_uses_real_mentions(self):
        self.auth.guard = lambda feature: AsyncMock(return_value=True)
        from fxbot.core.plugins import LoadedPlugin, PluginContext, PluginRuntime, PluginMetadata, collect_matchers
        from fxbot.core.scheduler import Scheduler
        from fxbot.plugins.group_admin import commands

        await self.app.stop()
        self.app = Application()
        self.addAsyncCleanup(self.app.stop)
        context = PluginContext(self.app, Scheduler(self.app), self.auth, AsyncMock(), self.auth.configuration)
        definition = LoadedPlugin("group_admin", PluginMetadata("群管理"), collect_matchers((commands,)), (), ())
        PluginRuntime(context, definition).install()
        await self.app.start()
        self.app.register(self.bot, close=AsyncMock())
        event = replace(
            self.event,
            message=Message(
                [MessageSegment.text("#禁言 "), MessageSegment.mention("400"), MessageSegment.text(" 10分钟")]
            ),
            original_message=None,
        )
        result = await self.app.router.dispatch(self.bot, event)
        self.assertIn("group_admin:mute_member", result.handled)
        self.assertEqual(self.mutations()[0][1]["duration"], 600)

    async def test_verification_fullwidth_success(self):
        verifier = self.verification()
        self.pending(verifier)
        await verifier.answer(self.context("２３４５"))
        self.assertEqual(verifier.pending, {})
        self.assertEqual(self.mutations(), [])

    async def test_verification_two_failures_kicks_once(self):
        self.roles["200"] = "member"
        verifier = self.verification()
        self.pending(verifier)
        await verifier.answer(self.context("1111"))
        self.assertEqual(self.mutations(), [])
        await verifier.answer(self.context("1111"))
        await verifier.answer(self.context("1111"))
        self.assertEqual([action for action, _ in self.mutations()], ["set_group_kick"])

    async def test_verification_timeout_protected_admin_not_kicked(self):
        verifier = self.verification()
        self.pending(verifier, deadline=time.monotonic() - 1)
        await verifier.sweep()
        self.assertEqual(verifier.pending, {})
        self.assertEqual(self.mutations(), [])

    async def test_verification_other_bot_same_group_not_pending(self):
        verifier = self.verification()
        self.pending(verifier)
        target = SessionTarget("onebot11", "101", "group", "300")
        other = replace(self.event, bot_id="101", target=target)
        self.assertFalse(verifier.has_pending(other))
        self.assertTrue(verifier.has_pending(self.event))

    async def test_verification_failed_send_rolls_back(self):
        self.roles["200"] = "member"
        verifier = self.verification()
        with (
            patch.object(self.bot, "send", side_effect=OSError("fake send")),
            patch("fxbot.plugins.group_admin.join_verify._render_captcha", return_value=b"png"),
        ):
            with self.assertRaises(OSError):
                await verifier.joined(self.context())
        self.assertEqual(verifier.pending, {})

    async def test_verification_leave_clears_pending(self):
        verifier = self.verification()
        self.pending(verifier)
        await verifier.left(self.context())
        await verifier.sweep()
        self.assertEqual(verifier.pending, {})

    async def test_banwords_scope_and_corruption(self):
        words = Banwords(self.service)
        words.directory = self.root
        data = words.load(GROUP)
        data["words"]["危险"] = {"match": "fuzzy", "penalty": "mute"}
        write_json(words.path(GROUP), data)
        other = SessionTarget("onebot11", "101", "group", "300")
        self.assertEqual(words.load(other)["words"], {})
        words.path(GROUP).write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            words.load(GROUP)
        self.assertEqual(words.path(GROUP).read_text(encoding="utf-8"), "{bad")

    async def test_like_is_one_bounded_request_per_actor_day(self):
        likes = Likes()
        likes.path = self.root / "likes.json"
        await likes.send(self.bot, "200")
        await likes.send(self.bot, "200")
        self.assertEqual(self.mutations(), [("send_like", {"user_id": 200, "times": 10})])

    async def test_existing_like_bot_user_structure_loads_without_rewriting(self):
        from fxbot.utils.tz import today_str

        likes = Likes()
        likes.path = self.root / "likes.json"
        data = {"100": {"200": {"date": today_str(), "count": 50}}}
        write_json(likes.path, data)
        previous = likes.path.read_bytes()
        self.assertEqual(likes.records(), data)
        self.assertIn("50", await likes.send(self.bot, "200"))
        self.assertEqual(self.mutations(), [])
        self.assertEqual(likes.path.read_bytes(), previous)

    async def test_concurrent_join_notices_create_one_challenge(self):
        verifier = self.verification()
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def member_role(group, user):
            if user == "100":
                entered.set()
                await release.wait()
                return "owner"
            return "member"

        self.bot.member_role = AsyncMock(side_effect=member_role)
        context = self.context()
        with patch("fxbot.plugins.group_admin.join_verify._render_captcha", return_value=b"image"):
            first = asyncio.create_task(verifier.joined(context))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(verifier.joined(context), 1)
            release.set()
            await asyncio.wait_for(first, 1)
        self.assertEqual(len(verifier.pending), 1)
        self.assertEqual(len([action for action, _ in self.calls if action == "send_group_msg"]), 1)

    async def test_member_leaving_during_identity_lookup_cancels_pending_challenge(self):
        verifier = self.verification()
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def member_role(group, user):
            entered.set()
            await release.wait()
            return "owner" if user == "100" else "member"

        self.bot.member_role = AsyncMock(side_effect=member_role)
        context = self.context()
        first = asyncio.create_task(verifier.joined(context))
        await asyncio.wait_for(entered.wait(), 1)
        await verifier.left(context)
        release.set()
        await asyncio.wait_for(first, 1)
        self.assertEqual(verifier.pending, {})
        self.assertEqual(self.calls, [])

    async def test_like_persists_bot_then_user_and_preserves_other_bots(self):
        likes = Likes()
        likes.path = self.root / "likes.json"
        previous = {"101": {"200": {"date": "2020-01-01", "count": 20}}}
        write_json(likes.path, previous)
        await likes.send(self.bot, "200")
        records = likes.records()
        self.assertEqual(records["101"], previous["101"])
        self.assertEqual(records["100"]["200"]["count"], 10)
        self.assertEqual(set(records), {"100", "101"})

    async def test_invalid_like_file_is_not_overwritten(self):
        likes = Likes()
        likes.path = self.root / "likes.json"
        write_json(likes.path, {"100": {"200": {"date": "2020-01-01", "count": True}}})
        previous = likes.path.read_bytes()
        with self.assertRaises(ValueError):
            await likes.send(self.bot, "200")
        self.assertEqual(self.calls, [])
        self.assertEqual(likes.path.read_bytes(), previous)

    async def test_existing_fortune_user_ids_load_without_rewriting(self):
        from fxbot.utils.tz import today_str
        from fxbot.plugins.entertain import fortune

        path = self.root / "fortunes.json"
        data = {"12EC7127C305E48A43D664129E856A8B": {"fortune": {"fortuneSummary": "吉"}, "time": today_str()}}
        write_json(path, data)
        previous = path.read_bytes()
        with patch.object(fortune, "USER_DATA_FILE", path), patch.object(fortune, "_USER_FORTUNES", {}):
            fortune._load_fortune_defs()
            fortune._load_user_fortunes()
            record, created = fortune._get_or_create_today_fortune(next(iter(data)))
            self.assertEqual(record, next(iter(data.values())))
            self.assertFalse(created)
        self.assertEqual(path.read_bytes(), previous)

    async def test_fortune_command_shares_user_record_between_bots(self):
        from fxbot.plugins.entertain import fortune

        path = self.root / "fortunes.json"
        other = replace(self.event, bot_id="101", target=SessionTarget("onebot11", "101", "group", "301"))
        with (
            patch.object(fortune, "USER_DATA_FILE", path),
            patch.object(fortune, "_USER_FORTUNES", {}),
            patch.object(fortune, "_JRYS_DATA", [{"fortuneSummary": "吉"}]),
            patch.object(fortune, "_get_background_image", return_value=None),
            patch.object(fortune, "_generate_fortune_canvas") as render,
            patch.object(fortune, "image_bytes", return_value=b"png"),
        ):
            await fortune.handle(self.context())
            await fortune.handle(HandlerContext(OneBot11Client("101", AsyncMock()), other, MatchResult("")))
            self.assertEqual(set(fortune._USER_FORTUNES), {"200"})
            self.assertEqual(render.call_args_list[0].args[1], render.call_args_list[1].args[1])

    async def test_fortune_tool_uses_user_id_not_music_owner(self):
        from fxbot.plugins.entertain import tools

        with (
            patch.object(tools, "authorized", return_value=(self.event, self.bot)),
            patch.object(
                tools.fortune, "_get_or_create_today_fortune", return_value=({"fortune": {}}, False)
            ) as get_record,
            patch.object(tools.fortune, "_get_background_image", return_value=None),
            patch.object(tools.fortune, "_generate_fortune_canvas"),
            patch.object(tools.fortune, "image_bytes", return_value=b"png"),
        ):
            await tools.get_fortune_tool(Mock(), Mock())
        get_record.assert_called_once_with("200")

    async def test_like_concurrent_requests_do_not_duplicate(self):
        likes = Likes()
        likes.path = self.root / "likes.json"
        await asyncio.gather(likes.send(self.bot, "200"), likes.send(self.bot, "200"))
        self.assertEqual(len(self.mutations()), 1)

    async def test_like_failed_action_does_not_mark_success(self):
        likes = Likes()
        likes.path = self.root / "likes.json"
        with patch.object(self.bot, "like", side_effect=OSError("fake")):
            with self.assertRaises(OSError):
                await likes.send(self.bot, "200")
        self.assertFalse(likes.path.exists())

    async def test_image_transform_preserves_input(self):
        display = ImageDisplay()
        message = Message([MessageSegment.image(b"png")])
        cfg = {"enabled": True, "type": 1, "text": "测试"}
        with patch("fxbot.plugins.entertain.image_display.cfg_image_display", return_value=cfg):
            result = display.transform(self.bot, GROUP, message)
        self.assertEqual(result.segments[0].data["summary"], "测试")
        self.assertNotIn("summary", message.segments[0].data)

    async def test_forward_node_fields_match_downloaded_adapter(self):
        await self.bot.send_forward(GROUP, [Message.text("测试"), Message([MessageSegment.image(b"png")])])
        action, args = self.calls[-1]
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual(set(args["messages"][0]["data"]), {"user_id", "nickname", "content"})
        self.assertTrue(args["messages"][1]["data"]["content"][0]["data"]["file"].startswith("base64://"))

    async def test_music_selection_scopes_actor_and_target(self):
        music = Music(self.app, self.auth)
        music.selections[music.selection_key(self.event)] = (time.monotonic() + 60, "qq", [Song("1", "歌", "人", "")])
        self.assertEqual(music.match_select(self.context("#1").event), "0")
        other = replace(self.context("#1").event, target=SessionTarget("onebot11", "100", "group", "301"))
        self.assertIsNone(music.match_select(other))
        self.assertIsNone(music.match_select(replace(self.context("#1").event, user_id="201")))

    async def test_music_expired_selection_falls_through(self):
        music = Music(self.app, self.auth)
        music.selections[music.selection_key(self.event)] = (0, "qq", [])
        self.assertIsNone(music.match_select(self.context("#1").event))

    def test_music_login_sessions_use_bare_owner(self):
        from fxbot.plugins.entertain import musicshare

        path = self.root / "music.json"
        write_json(
            path,
            {
                "qq:200": {
                    "provider": "qq",
                    "owner": "200",
                    "accounts": [{"auth": "a", "owner": "200", "provider": "qq"}],
                },
                "netease:onebot11:100:private:201": {
                    "provider": "netease",
                    "owner": "onebot11:100:private:201",
                    "accounts": [{"auth": "b", "owner": "onebot11:100:private:201", "provider": "netease"}],
                },
            },
        )
        with patch.object(musicshare, "_LOGIN_SESSION_FILE", path):
            sessions = musicshare._load_login_sessions()
        self.assertEqual(set(sessions), {"qq:200"})
        self.assertEqual(sessions["qq:200"]["owner"], "200")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("netease:onebot11:100:private:201", saved)

    async def test_music_group_login_is_allowed_and_scoped_to_user(self):
        from fxbot.plugins.entertain import musicshare

        music = Music(self.app, self.auth)
        private = SessionTarget("onebot11", "100", "private", "200")
        with (
            patch.object(musicshare, "_LOGIN_SESSION_FILE", self.root / "music.json"),
            patch.object(
                musicshare,
                "_music_api_get",
                return_value={"loginToken": "fake-token", "image": "data:image/png;base64,cG5n"},
            ),
        ):
            await music.login(self.context("#音乐登录"))
            await music.poll(self.context("#音乐登录状态"))
            await music.close()
        self.assertEqual(music.owner(self.event), music.owner(replace(self.event, target=private)))
        self.assertTrue(any(action == "send_group_msg" for action, _ in self.calls))

    async def test_music_login_api_failure_replies_instead_of_raising(self):
        import httpx

        from fxbot.core.router import _Finished
        from fxbot.plugins.entertain import musicshare

        request = httpx.Request("GET", "http://127.0.0.1:3000/api/login/qr")
        failure = httpx.HTTPStatusError("502", request=request, response=httpx.Response(502, request=request))
        music = Music(self.app, self.auth)
        with (
            patch.object(musicshare, "_LOGIN_SESSION_FILE", self.root / "music.json"),
            patch.object(musicshare, "_music_api_get", AsyncMock(side_effect=failure)),
        ):
            with self.assertRaises(_Finished):
                await music.login(self.context("#音乐登录"))
        action, params = self.calls[-1]
        self.assertEqual(action, "send_group_msg")
        self.assertIn("创建登录二维码失败", params["message"][0]["data"]["text"])

    async def test_music_close_cleans_pending_without_background_tasks(self):
        music = Music(self.app, self.auth)
        ctx = self.context("#音乐登录", target=SessionTarget("onebot11", "100", "private", "200"))
        module = "fxbot.plugins.entertain.musicshare"
        with (
            patch(module + "._LOGIN_SESSION_FILE", self.root / "music.json"),
            patch(
                module + "._music_api_get",
                return_value={"loginToken": "fake-token", "image": "data:image/png;base64,cG5n"},
            ),
        ):
            await music.login(ctx)
            self.assertTrue(music.pending)
            await music.close()
            self.assertEqual(music.pending, {})
            from fxbot.plugins.entertain.musicshare import _get_login_session_data

            bucket = await _get_login_session_data(music.owner(ctx.event), "qq")
            self.assertEqual(bucket["pending"], [])

    async def test_music_pending_poll_is_single_execution(self):
        music = Music(self.app, self.auth)
        module = "fxbot.plugins.entertain.musicshare"
        owner = music.owner(self.event)
        from fxbot.plugins.entertain.musicshare import _add_pending_login, _remove_pending_login

        with patch(module + "._LOGIN_SESSION_FILE", self.root / "music.json"):
            pending = await _add_pending_login(owner, "qq", "fake")

            async def complete(context, selected_owner, platform, record):
                await _remove_pending_login(selected_owner, platform, record["id"])
                return True

            with patch.object(music, "_poll_one", side_effect=complete) as poll:
                await asyncio.gather(
                    music.poll_one(self.context(), owner, "qq", pending),
                    music.poll_one(self.context(), owner, "qq", pending),
                )
            self.assertEqual(poll.await_count, 1)

    async def test_music_offline_login_is_removed_without_polling_remote_api(self):
        from fxbot.plugins.entertain import musicshare

        ctx = self.context("#音乐登录", target=SessionTarget("onebot11", "100", "private", "200"))
        with patch.object(musicshare, "_LOGIN_SESSION_FILE", self.root / "music.json"):
            music = Music(self.app, self.auth)
            owner = music.owner(ctx.event)
            pending = await musicshare._add_pending_login(owner, "qq", "fake")
            music.pending[(owner, "qq", pending["id"])] = (ctx, time.monotonic() + 60)
            self.app.unregister(self.bot)
            with patch.object(musicshare, "_music_api_get", new_callable=AsyncMock) as api:
                await music.check_pending()
            api.assert_not_awaited()
            self.assertFalse(music.pending)
            self.assertFalse((await musicshare._get_login_session_data(owner, "qq"))["pending"])

    async def test_music_scheduled_poll_confirms_account_then_removes_pending(self):
        from fxbot.plugins.entertain import musicshare

        ctx = self.context("#音乐登录", target=SessionTarget("onebot11", "100", "private", "200"))
        with patch.object(musicshare, "_LOGIN_SESSION_FILE", self.root / "music.json"):
            music = Music(self.app, self.auth)
            owner = music.owner(ctx.event)
            pending = await musicshare._add_pending_login(owner, "qq", "fake")
            music.pending[(owner, "qq", pending["id"])] = (ctx, time.monotonic() + 60)
            with patch.object(musicshare, "_music_api_get", return_value={"loggedIn": True, "auth": "fake-auth"}):
                await music.check_pending()
            self.assertFalse(music.pending)
            bucket = await musicshare._get_login_session_data(owner, "qq")
            self.assertFalse(bucket["pending"])
            self.assertTrue(any(account["auth"] == "fake-auth" for account in bucket["accounts"]))
            await music.close()

    async def test_music_revoked_during_poll_does_not_save_auth_or_leave_pending(self):
        from fxbot.plugins.entertain import musicshare

        ctx = self.context("#音乐登录", target=SessionTarget("onebot11", "100", "private", "200"))
        with patch.object(musicshare, "_LOGIN_SESSION_FILE", self.root / "music.json"):
            music = Music(self.app, self.auth)
            owner = music.owner(ctx.event)
            pending = await musicshare._add_pending_login(owner, "qq", "fake")
            music.pending[(owner, "qq", pending["id"])] = (ctx, time.monotonic() + 60)
            self.auth.check.side_effect = [True, False]
            with patch.object(musicshare, "_music_api_get", return_value={"loggedIn": True, "auth": "revoked-auth"}):
                await music.check_pending()
            self.assertFalse(music.pending)
            bucket = await musicshare._get_login_session_data(owner, "qq")
            self.assertFalse(bucket["pending"])
            self.assertFalse(any(account["auth"] == "revoked-auth" for account in bucket["accounts"]))

    async def test_music_shared_pool_uses_accounts_across_bots(self):
        from fxbot.plugins.entertain import musicshare

        owner = "200"
        other = "201"
        bucket = {"accounts": [{"auth": "fake-auth"}]}

        async def get_bucket(name, platform):
            return bucket if name == other else {"accounts": []}

        with (
            patch.object(musicshare, "_LOGIN_SESSIONS", {"qq:" + other: bucket}),
            patch.object(musicshare, "_get_login_session_data", side_effect=get_bucket),
            patch.object(musicshare, "_login_mode", return_value="shared"),
        ):
            candidates = await musicshare._auth_candidates(owner, "qq")
        self.assertEqual([item[0] for item in candidates], [other])

    async def test_welcome_rejects_legacy_or_traversal_data(self):
        welcome = Welcome(self.app, Mock())
        welcome.path = self.root / "welcome.json"
        for data in (
            {"300": {"enabled": True, "segments": []}},
            {GROUP.key(): {"enabled": True, "segments": [{"type": "image", "value": "../secret.png"}]}},
        ):
            write_json(welcome.path, data)
            before = welcome.path.read_bytes()
            with self.assertRaises(ValueError):
                welcome.records()
            self.assertEqual(welcome.path.read_bytes(), before)

    async def test_video_parse_error_cleans_owned_directory(self):
        plugin = video.VideoParser(self.app, self.auth)
        directory = self.root / "download"
        directory.mkdir()
        with (
            patch.object(video, "create_download_dir", return_value=directory),
            patch.object(video, "parse_url", side_effect=ParseError("fake")),
            patch.object(video, "cleanup_download_dir") as cleanup,
        ):
            await plugin.parse(self.context("https://b23.tv/test"))
        cleanup.assert_called_once_with(directory)
        self.assertEqual(plugin.downloads, set())

    async def test_video_cancellation_cleans_owned_directory(self):
        plugin = video.VideoParser(self.app, self.auth)
        directory = self.root / "download"
        with (
            patch.object(video, "create_download_dir", return_value=directory),
            patch.object(video, "parse_url", side_effect=asyncio.CancelledError),
            patch.object(video, "cleanup_download_dir") as cleanup,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await plugin.parse(self.context("https://b23.tv/test"))
        cleanup.assert_called_once_with(directory)
        self.assertEqual(plugin.downloads, set())

    async def test_video_revoked_permission_never_downloads(self):
        plugin = video.VideoParser(self.app, self.auth)
        self.auth.check.return_value = False
        with (
            patch.object(video, "create_download_dir", return_value=self.root / "download"),
            patch.object(
                video, "parse_url", return_value=VideoResult("bilibili", "测试", video_url="https://example.com/fake")
            ),
            patch.object(video, "cleanup_download_dir"),
            patch.object(video, "download_video", new_callable=AsyncMock) as download,
        ):
            await plugin.parse(self.context("https://b23.tv/test"))
        download.assert_not_awaited()

    async def test_video_sender_uses_explicit_transport_setting(self):
        path = self.root / "video.mp4"
        path.write_bytes(b"fake-video")
        result = VideoResult("bilibili", "测试")
        for inline in (True, False):
            with patch(
                "fxbot.plugins.video_parser.sender.cfg_general",
                return_value={"send_cover": False, "use_base64": inline},
            ):
                await send_video_result(self.context(), result, path)
            source = self.calls[-1][1]["messages"][-1]["data"]["content"][0]["data"]["file"]
            self.assertTrue(source.startswith("base64://" if inline else "file:///"))

    async def test_video_sender_degrades_to_separate_messages_without_forward(self):
        from fxbot.core.capabilities import Capabilities

        path = self.root / "video.mp4"
        path.write_bytes(b"fake-video")
        bot = SimpleNamespace(platform="qq", capabilities=Capabilities(), send=AsyncMock())
        context = HandlerContext(bot, self.event, MatchResult(""))
        result = VideoResult("bilibili", "测试", cover_url="https://example.com/cover.jpg")
        with patch(
            "fxbot.plugins.video_parser.sender.cfg_general",
            return_value={"send_cover": True, "use_base64": False},
        ):
            await send_video_result(context, result, path)
        self.assertEqual(bot.send.await_count, 3)
        self.assertEqual(bot.send.await_args_list[0].args[1].plain_text(), "bilibili | 测试")
        self.assertEqual(bot.send.await_args_list[1].args[1].segments[0].type, "image")
        self.assertEqual(bot.send.await_args_list[2].args[1].segments[0].data["file"], b"fake-video")

        bot.send.reset_mock()
        await send_image_result(context, VideoResult("bilibili", "图集"), [path, path])
        self.assertEqual(bot.send.await_count, 3)
        self.assertEqual(bot.send.await_args_list[-1].args[1].segments[0].type, "image")

    async def test_download_failure_does_not_retry_or_keep_partial_file(self):
        import httpx

        from fxbot.plugins.video_parser import downloader

        attempts = 0

        async def failed(url, *, file_path, headers):
            nonlocal attempts
            attempts += 1
            file_path.write_bytes(b"partial")
            raise httpx.ConnectError("fake transport")

        with patch.object(downloader, "_download_file_httpx", side_effect=failed):
            with self.assertRaises(downloader.DownloadError):
                await downloader.download_file("https://example.com/a.mp4", directory=self.root)
        self.assertEqual(attempts, 1)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_image_batch_waits_for_other_download_before_failure_cleanup(self):
        from fxbot.plugins.video_parser import downloader

        completed = []

        async def download(url, **kwargs):
            if url.endswith("1"):
                raise downloader.DownloadError("fake")
            await asyncio.sleep(0)
            completed.append(url)
            return self.root / "two.jpg"

        with patch.object(downloader, "download_file", side_effect=download):
            with self.assertRaises(downloader.DownloadError):
                await downloader.download_images(
                    VideoResult("test", "test", image_urls=["https://example.com/1", "https://example.com/2"]),
                    directory=self.root,
                )
        self.assertEqual(completed, ["https://example.com/2"])

    async def test_ffmpeg_cancel_kills_and_joins_process(self):
        from fxbot.plugins.video_parser import downloader

        process = Mock(returncode=None)
        process.wait = AsyncMock(side_effect=[asyncio.CancelledError, 0])
        with (
            patch.object(downloader.shutil, "which", return_value="fake-ffmpeg"),
            patch.object(downloader.asyncio, "create_subprocess_exec", return_value=process),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await downloader._merge_av(self.root / "v", self.root / "a", self.root / "out")
        process.kill.assert_called_once()
        self.assertEqual(process.wait.await_count, 2)

    async def test_cleanup_refuses_cache_root(self):
        from fxbot.plugins.video_parser import downloader

        with self.assertRaises(ValueError):
            downloader.cleanup_download_dir(downloader.CACHE_DIR)


class NativeValidationTests(unittest.TestCase):
    def test_duration_requires_unambiguous_input(self):
        self.assertEqual(parse_duration("10分钟"), 600)
        for text in ("差不多一会儿", "1h later", "-3"):
            with self.assertRaises(ValueError):
                parse_duration(text)

    def test_video_url_host_not_substring(self):
        for url in (
            "https://evil.example/?bilibili.com",
            "https://bilibili.com.evil.example/v",
            "https://bilibili.com@evil.example/v",
        ):
            self.assertFalse(can_parse_url(url))
        for url in ("https://www.bilibili.com/video/BV1xx411c7mD", "https://b23.tv/test", "av123456"):
            self.assertTrue(can_parse_url(url))
        self.assertEqual(find_url("观看 av123456"), "av123456")

    def test_configuration_does_not_coerce_bool_or_full_session_keys(self):
        for cfg in (
            {"box": {"only_admin": "false"}},
            {"box": {"auto_box_groups": ["onebot11:100:group:300"]}},
            {"music": {"login_mode": "own"}},
        ):
            with self.assertRaises(ValueError):
                validate_entertain(cfg)
        validate_entertain({"box": {"auto_box_groups": ["300"], "box_blacklist": ["200"]}})
        for cfg in (
            {"general": {"global_enabled": "false"}},
            {"platforms": {"bilibili": 1}},
            {"general": {"max_file_mb": float("inf")}},
        ):
            with self.assertRaises(ValueError):
                validate_video(cfg)


class NativePluginWebSocketTests(unittest.TestCase):
    def test_real_entry_routes_group_like_and_video_actions(self):
        from fastapi.testclient import TestClient

        from fxbot.config import SYSTEM_DEFAULTS, get_manager
        from fxbot.runtime.server import create_app
        from fxbot.runtime.settings import Settings
        from tests.runtime.test_server import HEADERS

        system = deepcopy(SYSTEM_DEFAULTS)
        system["chat"]["enabled"] = False
        result = VideoResult("bilibili", "离线测试", video_url="https://example.com/fake")

        async def download(result, *, directory):
            path = directory / "test.mp4"
            path.write_bytes(b"fake-video")
            return path

        with (
            patch.object(get_manager(), "get_system", side_effect=lambda: deepcopy(system)),
            patch.object(video, "parse_url", return_value=result),
            patch.object(video, "download_video", side_effect=download),
            patch(
                "fxbot.plugins.video_parser.sender.cfg_general", return_value={"send_cover": False, "use_base64": True}
            ),
        ):
            app = create_app(Settings("test-only"))
            with TestClient(app) as client:
                with client.websocket_connect("/onebot/v11/ws", headers=dict(HEADERS)) as websocket:
                    cases = (
                        (
                            [
                                {"type": "text", "data": {"text": "#禁言 "}},
                                {"type": "at", "data": {"qq": "400"}},
                                {"type": "text", "data": {"text": " 60秒"}},
                            ],
                            ["get_group_member_info"] * 3 + ["set_group_ban", "send_group_msg"],
                        ),
                        ([{"type": "text", "data": {"text": "#赞我"}}], ["send_like", "send_group_msg"]),
                        (
                            [{"type": "text", "data": {"text": "https://b23.tv/fake"}}],
                            ["send_group_msg", "send_group_forward_msg", "delete_msg"],
                        ),
                    )
                    for message, expected in cases:
                        incoming = payload()
                        incoming["message"] = message
                        websocket.send_json(incoming)
                        for name in expected:
                            action = websocket.receive_json()
                            self.assertEqual(action["action"], name)
                            data = {"message_id": 99}
                            if name == "get_group_member_info":
                                uid = action["params"]["user_id"]
                                data = {
                                    "user_id": uid,
                                    "role": "owner" if uid == 100 else "admin" if uid == 200 else "member",
                                }
                            elif name == "set_group_ban":
                                self.assertEqual(action["params"], {"group_id": 300, "user_id": 400, "duration": 60})
                            elif name == "send_like":
                                self.assertEqual(action["params"], {"user_id": 200, "times": 10})
                            elif name == "send_group_forward_msg":
                                self.assertEqual(action["params"]["group_id"], 300)
                                self.assertEqual(
                                    set(action["params"]["messages"][0]["data"]), {"user_id", "nickname", "content"}
                                )
                            websocket.send_json({"echo": action["echo"], "status": "ok", "retcode": 0, "data": data})
