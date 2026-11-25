"""gscore 双 Bot 身份映射：配对学习、路由决策与出站回程。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from fxbot.core import Event, Message, MessageSegment, SessionTarget
from fxbot.plugins.gscore import mapping as mapping_module
from fxbot.plugins.gscore.mapping import IdentityMap, Observation
from fxbot.plugins.gscore.models import Message as WireMessage, MessageReceive, MessageSend

CONFIG = {"mapping_enabled": True, "mapping_groups": ["300"]}


def event(
    platform: str,
    bot_id: str,
    target_id: str,
    user_id: str,
    name: str,
    text: str,
    second: int,
    *,
    nickname: str | None = None,
    images: int = 0,
) -> Event:
    target = SessionTarget(platform, bot_id, "group", target_id)
    segments = [MessageSegment.text(text)] if text else []
    segments.extend(MessageSegment.image(b"png") for _ in range(images))
    metadata = {"sender_nickname": nickname} if nickname is not None else {}
    return Event(
        None,
        f"{platform}-{second}-{user_id}",
        "message",
        platform,
        bot_id,
        user_id,
        name,
        target,
        Message(segments),
        metadata=metadata,
        occurred_at=datetime.fromtimestamp(second, timezone.utc),
    )


class MappingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.map_file = Path(directory.name) / "identity_map.json"
        patcher = patch.object(mapping_module, "MAP_FILE", self.map_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        config_patcher = patch.object(mapping_module, "get_config", return_value=CONFIG)
        config_patcher.start()
        self.addCleanup(config_patcher.stop)
        self.now = 0.0
        self.mapping = IdentityMap(clock=lambda: self.now)

    def onebot(self, second: int, *, text: str = "ww登录", uid: str = "3226795632", name: str = "友利奈绪") -> Event:
        return event("onebot11", "100", "300", uid, "🐯", text, second, nickname=name)

    def qq(self, second: int, *, text: str = "ww登录", openid: str = "A" * 32, name: str = "友利奈绪") -> Event:
        return event("qq", "102445077", "OPENID", openid, name, text, second)

    def test_five_nickname_hits_lock_user_and_group(self) -> None:
        for index in range(5):
            second = 1000 + index
            self.assertEqual(self.mapping.observe(self.onebot(second)).action, "forward")
            self.assertEqual(self.mapping.observe(self.qq(second)).action, "drop")
        self.assertEqual(self.mapping._users["A" * 32]["uin"], "3226795632")
        self.assertEqual(self.mapping._users["A" * 32]["qq_bot"], "102445077")
        self.assertEqual(self.mapping._groups["OPENID"]["onebot_group"], "300")
        self.assertEqual(self.mapping._groups["OPENID"]["qq_bot"], "102445077")

        self.assertEqual(self.mapping.observe(self.onebot(1006)).action, "drop")
        observation = self.mapping.observe(self.qq(1006))
        self.assertEqual(observation.action, "rewrite")
        self.assertEqual(observation.uin, "3226795632")
        self.assertEqual(observation.group, "300")
        self.assertEqual(observation.onebot_bot, "100")
        self.assertEqual(observation.qq_bot, "102445077")
        self.assertEqual(observation.qq_group, "OPENID")

    def test_nickname_mismatch_never_locks(self) -> None:
        for index in range(6):
            second = 2000 + index
            self.mapping.observe(self.onebot(second, name="不同昵称"))
            self.mapping.observe(self.qq(second, name="友利奈绪"))
        self.assertEqual(self.mapping._users, {})
        self.assertEqual(self.mapping._groups, {})
        self.assertEqual(self.mapping._user_hits, {})
        self.assertEqual(self.mapping._group_hits, {})

    def test_failed_match_resets_streak(self) -> None:
        for index in range(4):
            second = 3000 + index
            self.mapping.observe(self.onebot(second))
            self.mapping.observe(self.qq(second))
        self.now = 100
        self.mapping.observe(self.onebot(3100, text="孤立消息"))
        self.now = 104
        self.mapping.observe(self.onebot(3101, text="触发清扫", uid="999"))
        self.assertEqual(self.mapping._user_hits, {})
        self.assertEqual(self.mapping._group_hits, {})
        self.mapping.observe(self.onebot(3102))
        self.mapping.observe(self.qq(3102))
        self.assertEqual(self.mapping._users, {})
        for index in range(4):
            second = 3103 + index
            self.mapping.observe(self.onebot(second))
            self.mapping.observe(self.qq(second))
        self.assertIn("A" * 32, self.mapping._users)
        self.assertIn("OPENID", self.mapping._groups)

    def test_mapped_user_does_not_reset_group_streak(self) -> None:
        self.mapping._lock_user("A" * 32, "3226795632")
        for index in range(4):
            second = 6000 + index
            self.mapping.observe(self.onebot(second, uid="111", name="友利奈绪"))
            self.mapping.observe(self.qq(second, openid="B" * 32, name="友利奈绪"))
        self.now = 100
        self.mapping.observe(self.onebot(6100, text="孤立消息"))
        self.now = 104
        self.mapping.observe(self.onebot(6101, text="触发清扫", uid="999"))
        self.assertEqual(self.mapping._group_hits.get(("300", "OPENID")), 4)
        self.mapping.observe(self.onebot(6102, uid="111", name="友利奈绪"))
        self.mapping.observe(self.qq(6102, openid="B" * 32, name="友利奈绪"))
        self.assertIn("OPENID", self.mapping._groups)

    def test_hit_counters_survive_restart(self) -> None:
        for index in range(4):
            second = 4000 + index
            self.mapping.observe(self.onebot(second))
            self.mapping.observe(self.qq(second))
        self.mapping.close()
        reloaded = IdentityMap(clock=lambda: self.now)
        self.assertEqual(reloaded._user_hits[("A" * 32, "3226795632")], 4)
        reloaded.observe(self.onebot(4004))
        reloaded.observe(self.qq(4004))
        self.assertIn("A" * 32, reloaded._users)

    def test_ambiguous_window_is_not_learned(self) -> None:
        self.mapping.observe(self.qq(3000, openid="A" * 32, name="友利奈绪"))
        self.mapping.observe(self.qq(3000, openid="B" * 32, name="友利奈绪"))
        observation = self.mapping.observe(self.onebot(3000))
        self.assertEqual(observation.action, "forward")
        self.assertEqual(self.mapping._users, {})
        self.assertEqual(self.mapping._groups, {})

    def test_outgoing_reply_is_routed_back_to_official_bot(self) -> None:
        for index in range(5):
            second = 5000 + index
            self.mapping.observe(self.onebot(second))
            self.mapping.observe(self.qq(second))
        self.mapping.observe(self.onebot(5006))
        observation = self.mapping.observe(self.qq(5006))
        receive = MessageReceive(
            bot_id="qqgroup",
            bot_self_id="102445077",
            msg_id="official-msg",
            user_type="group",
            group_id="OPENID",
            user_id="A" * 32,
        )
        self.mapping.apply_receive(receive, observation)
        self.assertEqual(receive.bot_id, "onebot")
        self.assertEqual(receive.bot_self_id, "100")
        self.assertEqual(receive.user_id, "3226795632")
        self.assertEqual(receive.group_id, "300")
        self.mapping.remember_receive(receive, observation)
        outgoing = MessageSend(
            bot_id="onebot", bot_self_id="100", msg_id="official-msg", target_type="group", target_id="300"
        )
        rewritten = self.mapping.rewrite_outgoing(outgoing)
        self.assertEqual(rewritten.bot_id, "qqgroup")
        self.assertEqual(rewritten.bot_self_id, "102445077")
        self.assertEqual(rewritten.target_id, "OPENID")

    def test_rewrite_mentions_swaps_openids_and_bot_identity(self) -> None:
        self.mapping._lock_user("A" * 32, "3226795632")
        self.mapping._lock_user("B" * 32, "1492639107")
        self.mapping._lock_group("OPENID", "300", "100", "102445077")
        observation = Observation(
            action="rewrite",
            uin="3226795632",
            group="300",
            onebot_bot="100",
            qq_bot="102445077",
            qq_group="OPENID",
        )
        receive = MessageReceive(
            bot_id="qqgroup",
            bot_self_id="102445077",
            msg_id="official-msg",
            user_type="group",
            group_id="OPENID",
            user_id="A" * 32,
            content=[
                WireMessage("at", "B" * 32),
                WireMessage("at", "102445077"),
                WireMessage("at", "C" * 32),
                WireMessage("text", " wwkp"),
            ],
        )
        self.mapping.apply_receive(receive, observation)
        self.assertEqual(
            [segment.data for segment in receive.content],
            ["1492639107", "100", "C" * 32, " wwkp"],
        )

    def test_unmapped_message_keeps_original_route(self) -> None:
        outgoing = MessageSend(bot_id="onebot", bot_self_id="100", msg_id="unknown", target_type="group", target_id="300")
        self.assertIs(self.mapping.rewrite_outgoing(outgoing), outgoing)

    def test_disabled_mapping_ignores_events(self) -> None:
        with patch.object(mapping_module, "get_config", return_value={**CONFIG, "mapping_enabled": False}):
            self.assertEqual(self.mapping.observe(self.onebot(6000)).action, "default")
            self.assertEqual(self.mapping.observe(self.qq(6000)).action, "default")
        self.assertEqual(self.mapping._users, {})
