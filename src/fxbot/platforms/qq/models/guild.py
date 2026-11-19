"""QQ 官方频道、成员、私信和论坛数据结构。"""

import json
from enum import IntEnum
from typing import Generic, TypeVar
from datetime import datetime

from pydantic import BaseModel, TypeAdapter, field_validator, model_validator

from .common import MessageArk, MessageEmbed, MessageReference, MessageAttachment

T = TypeVar("T")


class Guild(BaseModel):
    id: str
    name: str
    icon: str
    owner_id: str
    owner: bool
    member_count: int
    max_members: int
    description: str
    joined_at: datetime


class User(BaseModel):
    id: str
    username: str | None = None
    avatar: str | None = None
    bot: bool | None = None
    union_openid: str | None = None
    union_user_account: str | None = None


class ChannelType(IntEnum):
    TEXT = 0
    VOICE = 2
    GROUP = 4
    LIVE = 10005
    APP = 10006
    DISCUSSION = 10007


class ChannelSubType(IntEnum):
    TALK = 0
    POST = 1
    CHEAT = 2
    BLACK = 3


class PrivateType(IntEnum):
    PUBLIC = 0
    ADMIN = 1
    SPECIFIED_USER = 2


class SpeakPermission(IntEnum):
    INVALID = 0
    EVERYONE = 1
    ADMIN = 2


class Channel(BaseModel):
    id: str
    guild_id: str
    name: str
    type: ChannelType | int
    sub_type: ChannelSubType | int
    position: int
    parent_id: str | None = None
    owner_id: str | None = None
    private_type: PrivateType | int | None = None
    speak_permission: SpeakPermission | int | None = None
    application_id: str | None = None
    permissions: int | None = None


class Member(BaseModel):
    user: User | None = None
    nick: str | None = None
    roles: list[str] | None = None
    joined_at: datetime


class GetRoleMembersReturn(BaseModel):
    data: list[Member]
    next: str


class Role(BaseModel):
    id: str
    name: str
    color: int
    hoist: bool
    number: int
    member_limit: int


class GetGuildRolesReturn(BaseModel):
    guild_id: str
    roles: list[Role]
    role_num_limit: int


class PostGuildRoleReturn(BaseModel):
    role_id: str
    role: Role


class PatchGuildRoleReturn(BaseModel):
    guild_id: str
    role_id: str
    role: Role


class ChannelPermissions(BaseModel):
    channel_id: str
    user_id: str | None = None
    role_id: str | None = None
    permissions: int


class Message(BaseModel):
    id: str
    channel_id: str
    guild_id: str
    content: str | None = None
    timestamp: datetime | None = None
    edited_timestamp: datetime | None = None
    mention_everyone: bool | None = None
    author: User
    attachments: list[MessageAttachment] | None = None
    embeds: list[MessageEmbed] | None = None
    mentions: list[User] | None = None
    member: Member | None = None
    ark: MessageArk | None = None
    seq: int | None = None
    seq_in_channel: str | None = None
    message_reference: MessageReference | None = None
    src_guild_id: str | None = None


class MessageDelete(BaseModel):
    message: Message
    op_user: User


class MessageSetting(BaseModel):
    disable_create_dm: bool
    disable_push_msg: bool
    channel_ids: list[str]
    channel_push_max_num: int


class DMS(BaseModel):
    guild_id: str | None = None
    channel_id: str | None = None
    create_time: datetime | None = None


class RecommendChannel(BaseModel):
    channel_id: str | None = None
    introduce: str | None = None


class Announces(BaseModel):
    guild_id: str | None = None
    channel_id: str | None = None
    message_id: str | None = None
    announces_type: int | None = None
    recommend_channels: list[RecommendChannel] | None = None


class PinsMessage(BaseModel):
    guild_id: str
    channel_id: str
    message_ids: list[str]


class RemindType(IntEnum):
    NO_REMIND = 0
    REMIND_START = 1
    REMIND_5_MIN = 2
    REMIND_15_MIN = 3
    REMIND_30_MIN = 4
    REMIND_60_MIN = 5


class Schedule(BaseModel):
    id: str
    name: str
    description: str | None = None
    start_timestamp: datetime
    end_timestamp: datetime
    creator: Member | None = None
    jump_channel_id: str | None = None
    remind_type: RemindType | int | None = None


class EmojiType(IntEnum):
    SYSTEM = 1
    CUSTOM = 2


class Emoji(BaseModel):
    id: str
    type: int


class ReactionTargetType(IntEnum):
    MESSAGE = 0
    FEED = 1
    COMMENT = 2
    REPLY = 3


class ReactionTarget(BaseModel):
    id: str
    type: ReactionTargetType | str


class MessageReaction(BaseModel):
    user_id: str
    guild_id: str
    channel_id: str
    target: ReactionTarget
    emoji: Emoji


class GetReactionUsersReturn(BaseModel):
    users: list[User]
    cookie: str
    is_end: bool


class AudioStatus(IntEnum):
    START = 0
    PAUSE = 1
    RESUME = 2
    STOP = 3


class AudioControl(BaseModel):
    audio_url: str | None = None
    text: str | None = None
    status: AudioStatus | int


class AudioAction(BaseModel):
    guild_id: str
    channel_id: str
    audio_url: str | None = None
    text: str | None = None


class ElemType(IntEnum):
    UNSUPPORTED = 0
    TEXT = 1
    IMAGE = 2
    VIDEO = 3
    URL = 4


class TextProps(BaseModel):
    font_bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None


class TextElem(BaseModel):
    text: str
    props: TextProps | None = None


class PlatImage(BaseModel):
    url: str | None = None
    width: int | None = None
    height: int | None = None
    image_id: str | None = None


class ImageElem(BaseModel):
    plat_image: PlatImage | None = None
    third_url: str | None = None
    width_percent: float | None = None


class VideoElem(BaseModel):
    third_url: str


class URLElem(BaseModel):
    url: str
    desc: str | None = None


class Elem(BaseModel):
    type: ElemType
    text: TextElem | None = None
    image: ImageElem | None = None
    video: VideoElem | None = None
    url: URLElem | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_type(cls, values: dict):
        if values.get("type") is not None:
            return values
        if values.get("text") is not None:
            values["type"] = ElemType.TEXT
        elif values.get("image") is not None:
            values["type"] = ElemType.IMAGE
        elif values.get("video") is not None:
            values["type"] = ElemType.VIDEO
        elif values.get("url") is not None:
            values["type"] = ElemType.URL
        else:
            values["type"] = ElemType.UNSUPPORTED
        return values


class Alignment(IntEnum):
    LEFT = 0
    MIDDLE = 1
    RIGHT = 2


class ParagraphProps(BaseModel):
    alignment: Alignment | None = None


class Paragraph(BaseModel):
    elems: list[Elem]
    props: ParagraphProps | None = None


class RichText(BaseModel):
    paragraphs: list[Paragraph] | None = None


class ThreadObjectInfo(BaseModel):
    thread_id: str
    content: RichText
    date_time: datetime

    @field_validator("content", mode="before")
    @classmethod
    def parse_content(cls, v):
        if isinstance(v, str):
            return TypeAdapter(RichText).validate_python(json.loads(v))
        return v


_T_Title = TypeVar("_T_Title", str, RichText)


class ThreadInfo(ThreadObjectInfo, Generic[_T_Title]):
    title: _T_Title

    @field_validator("title", mode="before")
    @classmethod
    def parse_title(cls, v):
        if isinstance(v, str) and cls.model_fields["title"].annotation is RichText:
            return TypeAdapter(RichText).validate_python(json.loads(v))
        return v


class ForumSourceInfo(BaseModel):
    guild_id: str
    channel_id: str
    author_id: str


class Thread(ForumSourceInfo, Generic[_T_Title]):
    thread_info: ThreadInfo[_T_Title]


class PostInfo(ThreadObjectInfo):
    post_id: str


class Post(ForumSourceInfo):
    post_info: PostInfo


class ReplyInfo(ThreadObjectInfo):
    post_id: str
    reply_id: str


class Reply(ForumSourceInfo):
    reply_info: ReplyInfo


class ForumAuditType(IntEnum):
    THREAD = 1
    POST = 2
    REPLY = 3


class ForumAuditResult(ForumSourceInfo):
    thread_id: str
    post_id: str
    reply_id: str
    type: ForumAuditType
    result: int | None = None
    err_msg: str | None = None


class GetThreadsListReturn(BaseModel):
    threads: list[Thread[str]]
    is_finish: bool


class GetThreadReturn(BaseModel):
    thread: Thread[str]


class PutThreadReturn(BaseModel):
    task_id: str
    create_time: datetime


class APIPermission(BaseModel):
    path: str
    method: str
    desc: str | None = None
    auth_status: bool


class APIPermissionDemandIdentify(BaseModel):
    path: str | None = None
    method: str | None = None


class APIPermissionDemand(BaseModel):
    guild_id: str
    channel_id: str
    api_identify: APIPermissionDemandIdentify
    title: str
    desc: str


class GetGuildAPIPermissionReturn(BaseModel):
    apis: list[APIPermission]


class UrlGetReturn(BaseModel):
    url: str


class SessionStartLimit(BaseModel):
    total: int
    remaining: int
    reset_after: int
    max_concurrency: int


class ShardUrlGetReturn(BaseModel):
    url: str
    shards: int
    session_start_limit: SessionStartLimit


__all__ = [
    "DMS",
    "APIPermission",
    "APIPermissionDemand",
    "APIPermissionDemandIdentify",
    "Alignment",
    "Announces",
    "AudioAction",
    "AudioControl",
    "AudioStatus",
    "Channel",
    "ChannelPermissions",
    "ChannelSubType",
    "ChannelType",
    "Elem",
    "ElemType",
    "Emoji",
    "EmojiType",
    "ForumAuditResult",
    "ForumAuditType",
    "ForumSourceInfo",
    "GetGuildAPIPermissionReturn",
    "GetGuildRolesReturn",
    "GetReactionUsersReturn",
    "GetRoleMembersReturn",
    "GetThreadReturn",
    "GetThreadsListReturn",
    "Guild",
    "ImageElem",
    "Member",
    "Message",
    "MessageDelete",
    "MessageReaction",
    "MessageSetting",
    "Paragraph",
    "ParagraphProps",
    "PatchGuildRoleReturn",
    "PinsMessage",
    "Post",
    "PostGuildRoleReturn",
    "PostInfo",
    "PrivateType",
    "PutThreadReturn",
    "ReactionTarget",
    "ReactionTargetType",
    "RecommendChannel",
    "RemindType",
    "Reply",
    "ReplyInfo",
    "RichText",
    "Role",
    "Schedule",
    "SessionStartLimit",
    "ShardUrlGetReturn",
    "SpeakPermission",
    "TextElem",
    "TextProps",
    "Thread",
    "ThreadInfo",
    "ThreadObjectInfo",
    "URLElem",
    "UrlGetReturn",
    "User",
    "VideoElem",
]
