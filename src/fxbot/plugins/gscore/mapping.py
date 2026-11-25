"""双 Bot 群身份映射：两侧消息配对学习、路由决策与出站回程。"""

from __future__ import annotations

import json
import time
from typing import Any, Literal
from dataclasses import replace, dataclass

from loguru import logger

from ...core import Event
from ...utils.paths import data_dir
from .config import get_config
from .models import MessageReceive, MessageSend

HIT_LIMIT = 5
WINDOW_SECONDS = 10.0
ROUTE_TTL = 900.0
MATCH_GRACE = 3.0
SAVE_INTERVAL = 2.0
MAP_FILE = data_dir("gscore") / "identity_map.json"


@dataclass(slots=True)
class Observation:
    """单个事件的路由结论；只有 rewrite 携带重写所需的身份。"""

    action: Literal["default", "forward", "drop", "rewrite"] = "default"
    uin: str = ""
    group: str = ""
    onebot_bot: str = ""
    qq_bot: str = ""
    qq_group: str = ""
    mentions: tuple[str, ...] = ()


@dataclass(slots=True)
class _Frame:
    """用于跨适配器配对的消息指纹，只保留可比较字段。"""

    platform: str
    group: str
    user: str
    name: str
    text: str
    images: int
    second: int
    ts: float
    bot_id: str
    forwarded: bool = False
    matched: bool = False
    swept: bool = False
    mentions: tuple[str, ...] = ()


def _name(value: str) -> str:
    return str(value or "").strip().casefold()


class IdentityMap:
    """学习结果与路由状态都属于应用；文件只保存已锁定的映射。"""

    def __init__(self, *, clock=None) -> None:
        self._clock = clock if clock is not None else time.monotonic
        self._groups: dict[str, dict[str, str]] = {}
        self._users: dict[str, dict[str, str]] = {}
        self._groups_rev: dict[str, str] = {}
        self._users_rev: dict[str, str] = {}
        self._group_hits: dict[tuple[str, str], int] = {}
        self._user_hits: dict[tuple[str, str], int] = {}
        self._onebot: list[_Frame] = []
        self._qq: list[_Frame] = []
        self._routes: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._last_save = float("-inf")
        self._load()

    # ---------- 配置与持久化 ----------

    @staticmethod
    def _configured_groups() -> set[str] | None:
        config = get_config()
        if not config["mapping_enabled"]:
            return None
        groups = {str(item) for item in config["mapping_groups"]}
        return groups or None

    def _load(self) -> None:
        try:
            data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        groups = data.get("groups") if isinstance(data, dict) else None
        users = data.get("users") if isinstance(data, dict) else None
        if isinstance(groups, dict):
            for openid, info in groups.items():
                if isinstance(info, dict) and isinstance(info.get("onebot_group"), str):
                    self._lock_group(
                        openid,
                        info["onebot_group"],
                        str(info.get("onebot_bot") or ""),
                        str(info.get("qq_bot") or ""),
                    )
        if isinstance(users, dict):
            for openid, info in users.items():
                if isinstance(info, dict) and isinstance(info.get("uin"), str):
                    self._lock_user(openid, info["uin"], str(info.get("qq_bot") or ""), persist=False)
        for openid, uin, hits in data.get("user_hits") or []:
            if isinstance(openid, str) and isinstance(uin, str) and type(hits) is int and hits > 0:
                self._user_hits[(openid, uin)] = hits
        for group, openid, hits in data.get("group_hits") or []:
            if isinstance(group, str) and isinstance(openid, str) and type(hits) is int and hits > 0:
                self._group_hits[(group, openid)] = hits

    def _save(self, *, force: bool = False) -> None:
        """计数变化最多每 2 秒落盘一次；锁定或关闭时强制立即写。"""
        self._dirty = True
        now = self._clock()
        if not force and now - self._last_save < SAVE_INTERVAL:
            return
        self._last_save = now
        self._dirty = False
        payload = {
            "version": 2,
            "groups": self._groups,
            "users": self._users,
            "user_hits": [[openid, uin, hits] for (openid, uin), hits in self._user_hits.items()],
            "group_hits": [[group, openid, hits] for (group, openid), hits in self._group_hits.items()],
        }
        try:
            MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = MAP_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(MAP_FILE)
        except OSError:
            logger.opt(exception=True).warning("[gscore] 身份映射保存失败")

    def _lock_group(self, openid: str, group: str, bot: str, qq_bot: str = "") -> bool:
        current = self._groups.get(openid) or {}
        if current.get("onebot_group") == group and (not qq_bot or current.get("qq_bot") == qq_bot):
            return False
        self._groups[openid] = {
            "onebot_group": group,
            "onebot_bot": bot,
            "qq_bot": qq_bot or str(current.get("qq_bot") or ""),
        }
        self._groups_rev[group] = openid
        return True

    def _lock_user(self, openid: str, uin: str, qq_bot: str = "", *, persist: bool = True) -> bool:
        current = self._users.get(openid) or {}
        if current.get("uin") == uin and (not qq_bot or current.get("qq_bot") == qq_bot):
            return False
        self._users[openid] = {"uin": uin, "qq_bot": qq_bot or str(current.get("qq_bot") or "")}
        self._users_rev[uin] = openid
        if persist:
            self._save()
        return True

    # ---------- 指纹与配对 ----------

    def _frame(self, event: Event, platform: str) -> _Frame:
        message = event.original_message if event.original_message is not None else event.message
        text = message.plain_text().strip()
        images = sum(1 for segment in message.segments if segment.type == "image")
        mentions = tuple(
            str(segment.data.get("user_id"))
            for segment in message.segments
            if segment.type == "mention" and segment.data.get("user_id") is not None
        )
        nickname = event.metadata.get("sender_nickname") if platform == "onebot" else event.user_name
        return _Frame(
            platform=platform,
            group=event.target.target_id,
            user=event.user_id,
            name=str(nickname or event.user_name or ""),
            text=text,
            images=images,
            second=int(event.occurred_at.timestamp()),
            ts=self._clock(),
            bot_id=event.bot_id,
            mentions=mentions,
        )

    @staticmethod
    def _prune(frames: list[_Frame], now: float) -> None:
        frames[:] = [frame for frame in frames if now - frame.ts <= WINDOW_SECONDS]

    def _sweep(self, frames: list[_Frame], now: float) -> None:
        """宽限期内没有配对的消息才算映射失败，避免先到的一方误清零。"""
        for frame in frames:
            if frame.swept or frame.matched or now - frame.ts <= MATCH_GRACE:
                continue
            frame.swept = True
            if frame.text:
                self._failure(frame)

    @staticmethod
    def _candidates(frame: _Frame, pool: list[_Frame]) -> list[_Frame]:
        return [
            item
            for item in pool
            if item.text
            and item.text == frame.text
            and item.images == frame.images
            and item.second == frame.second
        ]

    @staticmethod
    def _nickname_match(onebot: _Frame, qq: _Frame) -> bool:
        return bool(_name(onebot.name)) and _name(onebot.name) == _name(qq.name)

    def _hit(self, onebot: _Frame, qq: _Frame) -> None:
        """昵称+文本+同秒的唯一命中；连续 5 次才锁定，计数落盘。"""
        changed = False
        if qq.user not in self._users:
            key = (qq.user, onebot.user)
            self._user_hits[key] = self._user_hits.get(key, 0) + 1
            if self._user_hits[key] >= HIT_LIMIT:
                self._user_hits.pop(key, None)
                changed = self._lock_user(qq.user, onebot.user, qq.bot_id, persist=False) or changed
        if qq.group not in self._groups:
            key = (onebot.group, qq.group)
            self._group_hits[key] = self._group_hits.get(key, 0) + 1
            if self._group_hits[key] >= HIT_LIMIT:
                self._group_hits.pop(key, None)
                changed = self._lock_group(qq.group, onebot.group, onebot.bot_id, qq.bot_id) or changed
        self._save(force=changed)
        if changed:
            logger.info("[gscore] 身份映射更新: 群 {} 用户 {} 条", len(self._groups), len(self._users))

    def _failure(self, frame: _Frame) -> None:
        """本轮配不上：清掉该用户与所在群的所有连击，下一轮从零开始。"""
        changed = False
        if frame.platform == "onebot":
            for key in [key for key in self._user_hits if key[1] == frame.user]:
                self._user_hits.pop(key, None)
                changed = True
            for key in [key for key in self._group_hits if key[0] == frame.group]:
                self._group_hits.pop(key, None)
                changed = True
        else:
            for key in [key for key in self._user_hits if key[0] == frame.user]:
                self._user_hits.pop(key, None)
                changed = True
            for key in [key for key in self._group_hits if key[1] == frame.group]:
                self._group_hits.pop(key, None)
                changed = True
        if changed:
            self._save()

    def close(self) -> None:
        """进程退出前把被节流掉的计数补写一次。"""
        if self._dirty:
            self._save(force=True)

    # ---------- 观测与决策 ----------

    def observe(self, event: Event) -> Observation:
        groups = self._configured_groups()
        if groups is None or not event.is_group:
            return Observation()
        if event.platform == "onebot11":
            if event.target.target_id not in groups:
                return Observation()
            return self._observe_onebot(event)
        if event.platform == "qq":
            return self._observe_qq(event, groups)
        return Observation()

    def _observe_onebot(self, event: Event) -> Observation:
        now = self._clock()
        self._prune(self._qq, now)
        self._sweep(self._onebot, now)
        self._sweep(self._qq, now)
        frame = self._frame(event, "onebot")
        candidates = self._candidates(frame, self._qq)
        # 已映射用户不再参与学习，也不让它影响群连击。
        if frame.user in self._users_rev:
            frame.swept = True
        elif frame.text:
            if len(candidates) == 1 and self._nickname_match(frame, candidates[0]):
                frame.matched = candidates[0].matched = True
                self._hit(frame, candidates[0])
            elif candidates:
                frame.swept = True
                for item in candidates:
                    item.swept = True
                self._failure(frame)
        observation = Observation(action="forward")
        # 已映射用户且群映射已锁定：交给官方 Bot 处理，OneBot 侧只用于学习。
        if frame.user in self._users_rev and frame.group in self._groups_rev:
            observation.action = "drop"
        frame.forwarded = observation.action == "forward"
        self._onebot.append(frame)
        return observation

    def _observe_qq(self, event: Event, groups: set[str]) -> Observation:
        now = self._clock()
        self._prune(self._onebot, now)
        self._sweep(self._onebot, now)
        self._sweep(self._qq, now)
        frame = self._frame(event, "qq")
        pool = [item for item in self._onebot if item.group in groups]
        candidates = self._candidates(frame, pool)
        if frame.user in self._users:
            frame.swept = True
        elif frame.text:
            if len(candidates) == 1 and self._nickname_match(candidates[0], frame):
                frame.matched = candidates[0].matched = True
                self._hit(candidates[0], frame)
            elif candidates:
                frame.swept = True
                for item in candidates:
                    item.swept = True
                self._failure(frame)
        observation = Observation(action="default")
        group_info = self._groups.get(frame.group)
        onebot = candidates[0] if len(candidates) == 1 else None
        if group_info is None and onebot is None:
            self._qq.append(frame)
            return observation
        user_info = self._users.get(frame.user)
        if group_info is not None and user_info is not None:
            if onebot is not None and onebot.forwarded:
                observation.action = "drop"
            else:
                observation.action = "rewrite"
                observation.uin = user_info["uin"]
                observation.group = group_info["onebot_group"]
                observation.onebot_bot = group_info["onebot_bot"]
                observation.qq_bot = frame.bot_id
                observation.qq_group = frame.group
                # 官方副本的提及是 openid；同一条消息的 OneBot 副本里是 QQ 号。
                observation.mentions = onebot.mentions if onebot is not None else ()
        else:
            observation.action = "drop"
        frame.forwarded = observation.action == "rewrite"
        self._qq.append(frame)
        return observation

    # ---------- 入站改写与出站回程 ----------

    def apply_receive(self, receive: MessageReceive, observation: Observation) -> None:
        """把官方事件伪装成 OneBot 身份，让 gs 命中旧绑定。"""
        if observation.action != "rewrite":
            return
        official_bot = receive.bot_self_id
        receive.bot_id = "onebot"
        receive.bot_self_id = observation.onebot_bot
        receive.user_id = observation.uin
        receive.group_id = observation.group
        if isinstance(receive.sender, dict):
            receive.sender["user_id"] = observation.uin
        self._rewrite_mentions(receive, observation, official_bot)

    def _rewrite_mentions(self, receive: MessageReceive, observation: Observation, official_bot: str) -> None:
        """提及也要跟着换身份：官方 openid 换成 QQ 号，机器人的提及换成 OneBot Bot。"""
        segments = [segment for segment in receive.content or [] if segment.type == "at"]
        if not segments:
            return
        for segment in segments:
            if str(segment.data or "") in {observation.qq_bot, official_bot} and observation.onebot_bot:
                segment.data = observation.onebot_bot
        # 映射表里没有的人，用同一条消息 OneBot 副本的提及按顺序补；
        # 数量对不上说明配对不可靠（例如 @机器人 多出的合成段），只保留已知改写。
        if not observation.onebot_bot:
            return
        remaining = [segment for segment in segments if str(segment.data or "") != observation.onebot_bot]
        paired = [mention for mention in observation.mentions if mention and mention != observation.onebot_bot]
        aligned = len(remaining) == len(paired)
        for index, segment in enumerate(remaining):
            target = str(segment.data or "")
            info = self._users.get(target)
            if info is not None:
                segment.data = info["uin"]
            elif aligned:
                segment.data = paired[index]

    def remember_receive(self, receive: MessageReceive, observation: Observation) -> None:
        if observation.action != "rewrite" or not receive.msg_id:
            return
        now = self._clock()
        for key in [key for key, value in self._routes.items() if value["ts"] < now - ROUTE_TTL]:
            self._routes.pop(key, None)
        self._routes[receive.msg_id] = {
            "kind": "qq",
            "qq_bot": observation.qq_bot,
            "qq_group": observation.qq_group,
            "ts": now,
        }

    def rewrite_outgoing(self, message: MessageSend) -> MessageSend:
        """gs 对官方入站的回复按 msg_id 回程到官方 Bot。"""
        info = self._routes.get(message.msg_id) if message.msg_id else None
        if info is None or message.bot_id != "onebot":
            return message
        return replace(
            message,
            bot_id="qqgroup",
            bot_self_id=info["qq_bot"],
            target_type="group",
            target_id=info["qq_group"],
        )
