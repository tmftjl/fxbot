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


def _name(value: str) -> str:
    return str(value or "").strip().casefold()


class IdentityMap:
    """学习结果与路由状态都属于应用；文件只保存已锁定的映射。"""

    def __init__(self) -> None:
        self._groups: dict[str, dict[str, str]] = {}
        self._users: dict[str, dict[str, str]] = {}
        self._groups_rev: dict[str, str] = {}
        self._users_rev: dict[str, str] = {}
        self._group_hits: dict[tuple[str, str], int] = {}
        self._user_hits: dict[tuple[str, str], int] = {}
        self._onebot: list[_Frame] = []
        self._qq: list[_Frame] = []
        self._routes: dict[str, dict[str, Any]] = {}
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
                    self._lock_group(openid, info["onebot_group"], str(info.get("onebot_bot") or ""))
        if isinstance(users, dict):
            for openid, info in users.items():
                if isinstance(info, dict) and isinstance(info.get("uin"), str):
                    self._lock_user(openid, info["uin"], persist=False)

    def _save(self) -> None:
        payload = {"version": 1, "groups": self._groups, "users": self._users}
        try:
            MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = MAP_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(MAP_FILE)
        except OSError:
            logger.opt(exception=True).warning("[gscore] 身份映射保存失败")

    def _lock_group(self, openid: str, group: str, bot: str) -> bool:
        if self._groups.get(openid, {}).get("onebot_group") == group:
            return False
        self._groups[openid] = {"onebot_group": group, "onebot_bot": bot}
        self._groups_rev[group] = openid
        return True

    def _lock_user(self, openid: str, uin: str, *, persist: bool = True) -> bool:
        if self._users.get(openid, {}).get("uin") == uin:
            return False
        self._users[openid] = {"uin": uin}
        self._users_rev[uin] = openid
        if persist:
            self._save()
        return True

    # ---------- 指纹与配对 ----------

    @staticmethod
    def _frame(event: Event, platform: str) -> _Frame:
        message = event.original_message if event.original_message is not None else event.message
        text = message.plain_text().strip()
        images = sum(1 for segment in message.segments if segment.type == "image")
        nickname = event.metadata.get("sender_nickname") if platform == "onebot" else event.user_name
        return _Frame(
            platform=platform,
            group=event.target.target_id,
            user=event.user_id,
            name=str(nickname or event.user_name or ""),
            text=text,
            images=images,
            second=int(event.occurred_at.timestamp()),
            ts=time.monotonic(),
            bot_id=event.bot_id,
        )

    @staticmethod
    def _prune(frames: list[_Frame], now: float) -> None:
        frames[:] = [frame for frame in frames if now - frame.ts <= WINDOW_SECONDS]

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

    def _learn(self, onebot: _Frame, qq: _Frame) -> None:
        nickname_match = bool(_name(onebot.name)) and _name(onebot.name) == _name(qq.name)
        changed = False
        if qq.user not in self._users:
            if nickname_match:
                changed = self._lock_user(qq.user, onebot.user)
            else:
                key = (qq.user, onebot.user)
                self._user_hits[key] = self._user_hits.get(key, 0) + 1
                if self._user_hits[key] >= HIT_LIMIT:
                    self._user_hits.pop(key, None)
                    changed = self._lock_user(qq.user, onebot.user)
        if qq.group not in self._groups:
            key = (onebot.group, qq.group)
            self._group_hits[key] = self._group_hits.get(key, 0) + 1
            if self._group_hits[key] >= HIT_LIMIT:
                self._group_hits.pop(key, None)
                changed = self._lock_group(qq.group, onebot.group, onebot.bot_id) or changed
        if changed:
            self._save()
            logger.info("[gscore] 身份映射更新: 群 {} 用户 {} 条", len(self._groups), len(self._users))

    def _ambiguous(self, frame: _Frame, candidates: list[_Frame]) -> None:
        """同一窗口出现多个候选时丢弃本轮，并清掉相关连击。"""
        for item in candidates:
            if frame.platform == "onebot":
                self._user_hits.pop((item.user, frame.user), None)
                self._group_hits.pop((frame.group, item.group), None)
            else:
                self._user_hits.pop((frame.user, item.user), None)
                self._group_hits.pop((item.group, frame.group), None)

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
        now = time.monotonic()
        self._prune(self._qq, now)
        frame = self._frame(event, "onebot")
        candidates = self._candidates(frame, self._qq)
        if len(candidates) == 1:
            self._learn(frame, candidates[0])
        elif len(candidates) > 1:
            self._ambiguous(frame, candidates)
        observation = Observation(action="forward")
        # 已映射用户且群映射已锁定：交给官方 Bot 处理，OneBot 侧只用于学习。
        if frame.user in self._users_rev and frame.group in self._groups_rev:
            observation.action = "drop"
        frame.forwarded = observation.action == "forward"
        self._onebot.append(frame)
        return observation

    def _observe_qq(self, event: Event, groups: set[str]) -> Observation:
        now = time.monotonic()
        self._prune(self._onebot, now)
        frame = self._frame(event, "qq")
        pool = [item for item in self._onebot if item.group in groups]
        candidates = self._candidates(frame, pool)
        if len(candidates) == 1:
            self._learn(candidates[0], frame)
        elif len(candidates) > 1:
            self._ambiguous(frame, candidates)
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
        else:
            observation.action = "drop"
        frame.forwarded = observation.action == "rewrite"
        self._qq.append(frame)
        return observation

    # ---------- 入站改写与出站回程 ----------

    @staticmethod
    def apply_receive(receive: MessageReceive, observation: Observation) -> None:
        """把官方事件伪装成 OneBot 身份，让 gs 命中旧绑定。"""
        if observation.action != "rewrite":
            return
        receive.bot_id = "onebot"
        receive.bot_self_id = observation.onebot_bot
        receive.user_id = observation.uin
        receive.group_id = observation.group
        if isinstance(receive.sender, dict):
            receive.sender["user_id"] = observation.uin

    def remember_receive(self, receive: MessageReceive, observation: Observation) -> None:
        if observation.action != "rewrite" or not receive.msg_id:
            return
        now = time.monotonic()
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
