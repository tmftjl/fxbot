"""QQ 官方网关与回调事件模型。"""

from enum import Enum
from typing import ClassVar
from datetime import datetime

from pydantic import BaseModel

from .models import (
    Post,
    User,
    Guild,
    Reply,
    Member,
    Thread,
    Channel,
    Message as GuildMessage,
    RichText,
    QQMessage,
    AudioAction,
    JoinRequest,
    AutoApproved,
    FriendAuthor,
    MessageDelete,
    MessageAudited,
    QQReplyMessage,
    ForumSourceInfo,
    MessageReaction,
    ForumAuditResult,
    ButtonInteraction,
    GroupMemberAuthor,
)


class EventType(str, Enum):
    READY = "READY"
    RESUMED = "RESUMED"
    GUILD_CREATE = "GUILD_CREATE"
    GUILD_UPDATE = "GUILD_UPDATE"
    GUILD_DELETE = "GUILD_DELETE"
    CHANNEL_CREATE = "CHANNEL_CREATE"
    CHANNEL_UPDATE = "CHANNEL_UPDATE"
    CHANNEL_DELETE = "CHANNEL_DELETE"
    GUILD_MEMBER_ADD = "GUILD_MEMBER_ADD"
    GUILD_MEMBER_UPDATE = "GUILD_MEMBER_UPDATE"
    GUILD_MEMBER_REMOVE = "GUILD_MEMBER_REMOVE"
    MESSAGE_CREATE = "MESSAGE_CREATE"
    MESSAGE_DELETE = "MESSAGE_DELETE"
    MESSAGE_REACTION_ADD = "MESSAGE_REACTION_ADD"
    MESSAGE_REACTION_REMOVE = "MESSAGE_REACTION_REMOVE"
    DIRECT_MESSAGE_CREATE = "DIRECT_MESSAGE_CREATE"
    DIRECT_MESSAGE_DELETE = "DIRECT_MESSAGE_DELETE"
    OPEN_FORUM_THREAD_CREATE = "OPEN_FORUM_THREAD_CREATE"
    OPEN_FORUM_THREAD_UPDATE = "OPEN_FORUM_THREAD_UPDATE"
    OPEN_FORUM_THREAD_DELETE = "OPEN_FORUM_THREAD_DELETE"
    OPEN_FORUM_POST_CREATE = "OPEN_FORUM_POST_CREATE"
    OPEN_FORUM_POST_DELETE = "OPEN_FORUM_POST_DELETE"
    OPEN_FORUM_REPLY_CREATE = "OPEN_FORUM_REPLY_CREATE"
    OPEN_FORUM_REPLY_DELETE = "OPEN_FORUM_REPLY_DELETE"
    AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER = "AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER"
    AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT = "AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT"
    C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
    GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"
    GROUP_MESSAGE_CREATE = "GROUP_MESSAGE_CREATE"
    INTERACTION_CREATE = "INTERACTION_CREATE"
    MESSAGE_AUDIT_PASS = "MESSAGE_AUDIT_PASS"
    MESSAGE_AUDIT_REJECT = "MESSAGE_AUDIT_REJECT"
    FORUM_THREAD_CREATE = "FORUM_THREAD_CREATE"
    FORUM_THREAD_UPDATE = "FORUM_THREAD_UPDATE"
    FORUM_THREAD_DELETE = "FORUM_THREAD_DELETE"
    FORUM_POST_CREATE = "FORUM_POST_CREATE"
    FORUM_POST_DELETE = "FORUM_POST_DELETE"
    FORUM_REPLY_CREATE = "FORUM_REPLY_CREATE"
    FORUM_REPLY_DELETE = "FORUM_REPLY_DELETE"
    FORUM_PUBLISH_AUDIT_RESULT = "FORUM_PUBLISH_AUDIT_RESULT"
    AUDIO_START = "AUDIO_START"
    AUDIO_FINISH = "AUDIO_FINISH"
    AUDIO_ON_MIC = "AUDIO_ON_MIC"
    AUDIO_OFF_MIC = "AUDIO_OFF_MIC"
    AT_MESSAGE_CREATE = "AT_MESSAGE_CREATE"
    PUBLIC_MESSAGE_DELETE = "PUBLIC_MESSAGE_DELETE"
    FRIEND_ADD = "FRIEND_ADD"
    FRIEND_DEL = "FRIEND_DEL"
    C2C_MSG_REJECT = "C2C_MSG_REJECT"
    C2C_MSG_RECEIVE = "C2C_MSG_RECEIVE"
    GROUP_ADD_ROBOT = "GROUP_ADD_ROBOT"
    GROUP_DEL_ROBOT = "GROUP_DEL_ROBOT"
    GROUP_MSG_REJECT = "GROUP_MSG_REJECT"
    GROUP_MSG_RECEIVE = "GROUP_MSG_RECEIVE"
    GROUP_MEMBER_ADD = "GROUP_MEMBER_ADD"
    GROUP_MEMBER_REMOVE = "GROUP_MEMBER_REMOVE"
    GROUP_JOIN_REQUEST = "GROUP_JOIN_REQUEST"


class Event(BaseModel):
    __type__: ClassVar[EventType]
    event_id: str | None = None


class MetaEvent(Event):
    pass


class ReadyEvent(MetaEvent):
    __type__ = EventType.READY
    version: int
    session_id: str
    user: User
    shard: tuple[int, int]


class ResumedEvent(MetaEvent):
    __type__ = EventType.RESUMED


class NoticeEvent(Event):
    pass


class GuildEvent(NoticeEvent, Guild):
    op_user_id: str


class GuildCreateEvent(GuildEvent):
    __type__ = EventType.GUILD_CREATE


class GuildUpdateEvent(GuildEvent):
    __type__ = EventType.GUILD_UPDATE


class GuildDeleteEvent(GuildEvent):
    __type__ = EventType.GUILD_DELETE


class ChannelEvent(NoticeEvent, Channel):
    op_user_id: str


class ChannelCreateEvent(ChannelEvent):
    __type__ = EventType.CHANNEL_CREATE


class ChannelUpdateEvent(ChannelEvent):
    __type__ = EventType.CHANNEL_UPDATE


class ChannelDeleteEvent(ChannelEvent):
    __type__ = EventType.CHANNEL_DELETE


class GuildMemberEvent(NoticeEvent, Member):
    guild_id: str
    op_user_id: str


class GuildMemberAddEvent(GuildMemberEvent):
    __type__ = EventType.GUILD_MEMBER_ADD


class GuildMemberUpdateEvent(GuildMemberEvent):
    __type__ = EventType.GUILD_MEMBER_UPDATE


class GuildMemberRemoveEvent(GuildMemberEvent):
    __type__ = EventType.GUILD_MEMBER_REMOVE


class MessageEvent(Event):
    to_me: bool = False


class GuildMessageEvent(MessageEvent, GuildMessage):
    reply: GuildMessage | None = None


class MessageCreateEvent(GuildMessageEvent):
    __type__ = EventType.MESSAGE_CREATE


class MessageDeleteEvent(NoticeEvent, MessageDelete):
    __type__ = EventType.MESSAGE_DELETE


class AtMessageCreateEvent(GuildMessageEvent):
    __type__ = EventType.AT_MESSAGE_CREATE
    to_me: bool = True


class PublicMessageDeleteEvent(MessageDeleteEvent):
    __type__ = EventType.PUBLIC_MESSAGE_DELETE


class DirectMessageCreateEvent(GuildMessageEvent):
    __type__ = EventType.DIRECT_MESSAGE_CREATE
    to_me: bool = True


class DirectMessageDeleteEvent(MessageDeleteEvent):
    __type__ = EventType.DIRECT_MESSAGE_DELETE


class QQMessageEvent(MessageEvent, QQMessage):
    reply: QQReplyMessage | None = None
    _reply_seq: int = 0


class C2CMessageCreateEvent(QQMessageEvent):
    __type__ = EventType.C2C_MESSAGE_CREATE
    author: FriendAuthor
    to_me: bool = True


class GroupMessageCreateEvent(QQMessageEvent):
    __type__ = EventType.GROUP_MESSAGE_CREATE
    author: GroupMemberAuthor
    group_id: str
    group_openid: str


class GroupAtMessageCreateEvent(GroupMessageCreateEvent):
    __type__ = EventType.GROUP_AT_MESSAGE_CREATE
    to_me: bool = True


class InteractionCreateEvent(NoticeEvent, ButtonInteraction):
    __type__ = EventType.INTERACTION_CREATE


class MessageAuditEvent(NoticeEvent, MessageAudited): ...


class MessageAuditPassEvent(MessageAuditEvent):
    __type__ = EventType.MESSAGE_AUDIT_PASS


class MessageAuditRejectEvent(MessageAuditEvent):
    __type__ = EventType.MESSAGE_AUDIT_REJECT


class MessageReactionEvent(NoticeEvent, MessageReaction):
    pass


class MessageReactionAddEvent(MessageReactionEvent):
    __type__ = EventType.MESSAGE_REACTION_ADD


class MessageReactionRemoveEvent(MessageReactionEvent):
    __type__ = EventType.MESSAGE_REACTION_REMOVE


class AudioEvent(NoticeEvent, AudioAction): ...


class AudioStartEvent(AudioEvent):
    __type__ = EventType.AUDIO_START


class AudioFinishEvent(AudioEvent):
    __type__ = EventType.AUDIO_FINISH


class AudioOnMicEvent(AudioEvent):
    __type__ = EventType.AUDIO_ON_MIC


class AudioOffMicEvent(AudioEvent):
    __type__ = EventType.AUDIO_OFF_MIC


class ForumEvent(NoticeEvent, ForumSourceInfo):
    pass


class ForumThreadEvent(ForumEvent, Thread[RichText]): ...


class ForumThreadCreateEvent(ForumThreadEvent):
    __type__ = EventType.FORUM_THREAD_CREATE


class ForumThreadUpdateEvent(ForumThreadEvent):
    __type__ = EventType.FORUM_THREAD_UPDATE


class ForumThreadDeleteEvent(ForumThreadEvent):
    __type__ = EventType.FORUM_THREAD_DELETE


class ForumPostEvent(ForumEvent, Post): ...


class ForumPostCreateEvent(ForumPostEvent):
    __type__ = EventType.FORUM_POST_CREATE


class ForumPostDeleteEvent(ForumPostEvent):
    __type__ = EventType.FORUM_POST_DELETE


class ForumReplyEvent(ForumEvent, Reply): ...


class ForumReplyCreateEvent(ForumReplyEvent):
    __type__ = EventType.FORUM_REPLY_CREATE


class ForumReplyDeleteEvent(ForumReplyEvent):
    __type__ = EventType.FORUM_REPLY_DELETE


class ForumPublishAuditResult(ForumEvent, ForumAuditResult):
    __type__ = EventType.FORUM_PUBLISH_AUDIT_RESULT


class OpenForumEvent(NoticeEvent, ForumSourceInfo):
    pass


class OpenForumThreadCreateEvent(OpenForumEvent):
    __type__ = EventType.OPEN_FORUM_THREAD_CREATE


class OpenForumThreadUpdateEvent(OpenForumEvent):
    __type__ = EventType.OPEN_FORUM_THREAD_UPDATE


class OpenForumThreadDeleteEvent(OpenForumEvent):
    __type__ = EventType.OPEN_FORUM_THREAD_DELETE


class OpenForumPostCreateEvent(OpenForumEvent):
    __type__ = EventType.OPEN_FORUM_POST_CREATE


class OpenForumPostDeleteEvent(OpenForumEvent):
    __type__ = EventType.OPEN_FORUM_POST_DELETE


class OpenForumReplyCreateEvent(OpenForumEvent):
    __type__ = EventType.OPEN_FORUM_REPLY_CREATE


class OpenForumReplyDeleteEvent(OpenForumEvent):
    __type__ = EventType.OPEN_FORUM_REPLY_DELETE


class FriendRobotEvent(NoticeEvent):
    timestamp: datetime
    openid: str


class FriendAddEvent(FriendRobotEvent):
    __type__ = EventType.FRIEND_ADD


class FriendDelEvent(FriendRobotEvent):
    __type__ = EventType.FRIEND_DEL


class C2CMsgRejectEvent(FriendRobotEvent):
    __type__ = EventType.C2C_MSG_REJECT


class C2CMsgReceiveEvent(FriendRobotEvent):
    __type__ = EventType.C2C_MSG_RECEIVE


class GroupRobotEvent(NoticeEvent):
    timestamp: datetime
    group_openid: str
    op_member_openid: str


class GroupAddRobotEvent(GroupRobotEvent):
    __type__ = EventType.GROUP_ADD_ROBOT


class GroupDelRobotEvent(GroupRobotEvent):
    __type__ = EventType.GROUP_DEL_ROBOT


class GroupMsgRejectEvent(GroupRobotEvent):
    __type__ = EventType.GROUP_MSG_REJECT


class GroupMsgReceiveEvent(GroupRobotEvent):
    __type__ = EventType.GROUP_MSG_RECEIVE


class GroupMemberEvent(NoticeEvent):
    timestamp: datetime
    group_openid: str
    member_openid: str


class GroupMemberAddEvent(GroupMemberEvent):
    __type__ = EventType.GROUP_MEMBER_ADD


class GroupMemberRemoveEvent(GroupMemberEvent):
    __type__ = EventType.GROUP_MEMBER_REMOVE


class GroupJoinRequestEvent(NoticeEvent, JoinRequest):
    __type__ = EventType.GROUP_JOIN_REQUEST
    group_openid: str
    auto_approved: AutoApproved | None = None


EVENT_CLASSES = {
    cls.__type__.value: cls
    for cls in tuple(globals().values())
    if isinstance(cls, type) and issubclass(cls, Event) and "__type__" in cls.__dict__
}
