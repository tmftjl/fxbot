"""QQ 官方 API 请求构造，只编码协议字段，不承担传输和重试。"""

from __future__ import annotations

import json
from base64 import b64encode
from typing import IO, Any, Literal
from datetime import datetime, timezone, timedelta

from pydantic import BaseModel

from .codec import api_path
from .models import (
    Media,
    RichText,
    EmojiType,
    MessageArk,
    RemindType,
    AudioStatus,
    ChannelType,
    PrivateType,
    AudioControl,
    MessageEmbed,
    MessageStream,
    ChannelSubType,
    MessageKeyboard,
    MessageMarkdown,
    SpeakPermission,
    MessageReference,
    RecommendChannel,
    SetMemberMuteState,
    MessageActionButton,
    MessagePromptKeyboard,
    APIPermissionDemandIdentify,
)
from .uploads import upload
from ...core.capabilities import CapabilityKey


def _payload(value, *, exclude_unset=False):
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True, exclude_unset=exclude_unset)
    if isinstance(value, dict):
        return {k: _payload(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_payload(v) for v in value]
    return value


class QQActions:
    def __init__(self, request, put_upload):
        self._request = request
        self._put_upload = put_upload

    async def put_upload_part(self, *, presigned_url: str, data: bytes) -> None:
        await self._put_upload(presigned_url, data)

    async def post_c2c_upload(
        self, openid: str, file_type: Literal[1, 2, 3, 4], file_name: str, file_data: bytes, srv_send_msg: bool = True
    ) -> dict[str, Any]:
        return await upload(self, "users", openid, file_type, file_name, file_data, srv_send_msg)

    async def post_group_upload(
        self,
        group_openid: str,
        file_type: Literal[1, 2, 3, 4],
        file_name: str,
        file_data: bytes,
        srv_send_msg: bool = True,
    ) -> dict[str, Any]:
        return await upload(self, "groups", group_openid, file_type, file_name, file_data, srv_send_msg)

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", api_path("users", "@me"))

    async def guilds(
        self, *, before: str | None = None, after: str | None = None, limit: float | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            api_path("users", "@me", "guilds"),
            params=_payload({"before": before, "after": after, "limit": limit}),
        )

    async def get_guild(self, *, guild_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("guilds", guild_id))

    async def get_channels(self, *, guild_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", api_path("guilds", guild_id, "channels"))

    async def get_channel(self, *, channel_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("channels", channel_id))

    async def post_channels(
        self,
        *,
        guild_id: str,
        name: str,
        type: ChannelType | int,
        sub_type: ChannelSubType | int,
        position: int | None = None,
        parent_id: int | None = None,
        private_type: PrivateType | int | None = None,
        private_user_ids: list[str] | None = None,
        speak_permission: SpeakPermission | int | None = None,
        application_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("guilds", guild_id, "channels"),
            json=_payload(
                {
                    "name": name,
                    "type": type,
                    "sub_type": sub_type,
                    "position": position,
                    "parent_id": parent_id,
                    "private_type": private_type,
                    "private_user_ids": private_user_ids,
                    "speaking_permission": speak_permission,
                    "application_id": application_id,
                }
            ),
        )

    async def patch_channel(
        self,
        *,
        channel_id: str,
        name: str | None = None,
        type: ChannelType | int | None = None,
        sub_type: ChannelSubType | int | None = None,
        position: int | None = None,
        parent_id: int | None = None,
        private_type: int | None = None,
        speak_permission: SpeakPermission | int | None = None,
        application_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            api_path("channels", channel_id),
            json=_payload(
                {
                    "name": name,
                    "type": type,
                    "sub_type": sub_type,
                    "position": position,
                    "parent_id": parent_id,
                    "private_type": private_type,
                    "speaking_permission": speak_permission,
                    "application_id": application_id,
                }
            ),
        )

    async def delete_channel(self, *, channel_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("channels", channel_id))

    async def get_members(
        self, *, guild_id: str, after: str | None = None, limit: float | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET", api_path("guilds", guild_id, "members"), params=_payload({"after": after, "limit": limit})
        )

    async def get_role_members(
        self, *, guild_id: str, role_id: str, start_index: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            api_path("guilds", guild_id, "roles", role_id, "members"),
            params=_payload({"start_index": start_index, "limit": limit}),
        )

    async def get_member(self, *, guild_id: str, user_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("guilds", guild_id, "members", user_id))

    async def delete_member(
        self,
        *,
        guild_id: str,
        user_id: str,
        add_blacklist: bool | None = None,
        delete_history_msg_days: Literal[-1, 0, 3, 7, 15, 30] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            api_path("guilds", guild_id, "members", user_id),
            json=_payload({"add_blacklist": add_blacklist, "delete_history_msg_days": delete_history_msg_days}),
        )

    async def get_guild_roles(self, *, guild_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("guilds", guild_id, "roles"))

    async def post_guild_role(
        self, *, guild_id: str, name: str | None = None, color: float | None = None, hoist: bool | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("guilds", guild_id, "roles"),
            json=_payload({"name": name, "color": color, "hoist": int(hoist) if hoist is not None else None}),
        )

    async def patch_guild_role(
        self,
        *,
        guild_id: str,
        role_id: str,
        name: str | None = None,
        color: float | None = None,
        hoist: bool | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            api_path("guilds", guild_id, "roles", role_id),
            json=_payload({"name": name, "color": color, "hoist": int(hoist) if hoist is not None else None}),
        )

    async def delete_guild_role(self, *, guild_id: str, role_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("guilds", guild_id, "roles", role_id))

    async def put_guild_member_role(
        self, *, guild_id: str, role_id: str, user_id: str, channel_id: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            api_path("guilds", guild_id, "members", user_id, "roles", role_id),
            json={"channel": {"id": channel_id}} if channel_id is not None else None,
        )

    async def delete_guild_member_role(
        self, *, guild_id: str, role_id: str, user_id: str, channel_id: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            api_path("guilds", guild_id, "members", user_id, "roles", role_id),
            json={"channel": {"id": channel_id}} if channel_id is not None else None,
        )

    async def get_channel_permissions(self, *, channel_id: str, user_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("channels", channel_id, "members", user_id, "permissions"))

    async def put_channel_permissions(
        self, *, channel_id: str, user_id: str, add: int | None = None, remove: int | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            api_path("channels", channel_id, "members", user_id, "permissions"),
            json=_payload({"add": str(add), "remove": str(remove)}),
        )

    async def get_channel_roles_permissions(self, *, channel_id: str, role_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("channels", channel_id, "roles", role_id, "permissions"))

    async def put_channel_roles_permissions(
        self, *, channel_id: str, role_id: str, add: int | None = None, remove: int | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            api_path("channels", channel_id, "roles", role_id, "permissions"),
            json=_payload({"add": str(add), "remove": str(remove)}),
        )

    async def get_message_of_id(self, *, channel_id: str, message_id: str) -> dict[str, Any]:
        result = await self._request("GET", api_path("channels", channel_id, "messages", message_id))
        if isinstance(result, dict) and "message" in result:
            result = result["message"]
        return result

    @staticmethod
    def _parse_send_message(data: dict[str, Any]) -> dict[str, Any]:
        data = _payload(data)
        data = {k: _payload(v) if isinstance(v, BaseModel) else v for k, v in data.items()}
        if file_image := data.pop("file_image", None):
            multipart_files: dict[str, Any] = {"file_image": ("file_image", file_image)}
            multipart_data: dict[str, Any] = {}
            for k, v in data.items():
                if isinstance(v, (dict, list)):
                    multipart_files[k] = (k, json.dumps({k: v}).encode("utf-8"), "application/json")
                else:
                    multipart_data[k] = v
            params = {"files": multipart_files, "data": multipart_data}
        else:
            params = {"json": data}
        return params

    async def post_messages(
        self,
        *,
        channel_id: str,
        content: str | None = None,
        embed: MessageEmbed | None = None,
        ark: MessageArk | None = None,
        message_reference: MessageReference | None = None,
        image: str | None = None,
        file_image: bytes | IO[bytes] | None = None,
        markdown: MessageMarkdown | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
        keyboard: MessageKeyboard | None = None,
    ) -> dict[str, Any]:
        params = self._parse_send_message(
            {
                "content": content,
                "embed": embed,
                "ark": ark,
                "message_reference": message_reference,
                "image": image,
                "file_image": file_image,
                "markdown": markdown,
                "msg_id": msg_id,
                "event_id": event_id,
                "keyboard": keyboard,
            }
        )
        return await self._request("POST", api_path("channels", channel_id, "messages"), **params)

    async def delete_message(self, *, channel_id: str, message_id: str, hidetip: bool | None = None) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            api_path("channels", channel_id, "messages", message_id),
            params={"hidetip": str(hidetip).lower()} if hidetip is not None else None,
        )

    async def get_message_setting(self, *, guild_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("guilds", guild_id, "message", "setting"))

    async def post_dms(self, *, recipient_id: str, source_guild_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("users", "@me", "dms"),
            json=_payload({"recipient_id": recipient_id, "source_guild_id": source_guild_id}),
        )

    async def post_dms_messages(
        self,
        *,
        guild_id: str,
        content: str | None = None,
        embed: MessageEmbed | None = None,
        ark: MessageArk | None = None,
        message_reference: MessageReference | None = None,
        image: str | None = None,
        file_image: bytes | IO[bytes] | None = None,
        markdown: MessageMarkdown | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
        keyboard: MessageKeyboard | None = None,
    ) -> dict[str, Any]:
        params = self._parse_send_message(
            {
                "content": content,
                "embed": embed,
                "ark": ark,
                "message_reference": message_reference,
                "image": image,
                "file_image": file_image,
                "markdown": markdown,
                "msg_id": msg_id,
                "event_id": event_id,
                "keyboard": keyboard,
            }
        )
        return await self._request("POST", api_path("dms", guild_id, "messages"), **params)

    async def delete_dms_message(
        self, *, guild_id: str, message_id: str, hidetip: bool | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            api_path("dms", guild_id, "messages", message_id),
            params={"hidetip": str(hidetip).lower()} if hidetip is not None else None,
        )

    async def patch_guild_mute(
        self,
        *,
        guild_id: str,
        mute_end_timestamp: int | datetime | None = None,
        mute_seconds: int | timedelta | None = None,
    ) -> dict[str, Any]:
        if isinstance(mute_end_timestamp, datetime):
            mute_end_timestamp = int(mute_end_timestamp.timestamp())
        if isinstance(mute_seconds, timedelta):
            mute_seconds = int(mute_seconds.total_seconds())
        return await self._request(
            "PATCH",
            api_path("guilds", guild_id, "mute"),
            json=_payload({"mute_end_timestamp": str(mute_end_timestamp), "mute_seconds": str(mute_seconds)}),
        )

    async def patch_guild_member_mute(
        self,
        *,
        guild_id: str,
        user_id: str,
        mute_end_timestamp: int | datetime | None = None,
        mute_seconds: int | timedelta | None = None,
    ) -> dict[str, Any]:
        if isinstance(mute_end_timestamp, datetime):
            mute_end_timestamp = int(mute_end_timestamp.timestamp())
        if isinstance(mute_seconds, timedelta):
            mute_seconds = int(mute_seconds.total_seconds())
        return await self._request(
            "PATCH",
            api_path("guilds", guild_id, "members", user_id, "mute"),
            json=_payload({"mute_end_timestamp": str(mute_end_timestamp), "mute_seconds": str(mute_seconds)}),
        )

    async def patch_guild_mute_multi_member(
        self,
        *,
        guild_id: str,
        user_ids: list[str],
        mute_end_timestamp: int | datetime | None = None,
        mute_seconds: int | timedelta | None = None,
    ) -> list[int]:
        if isinstance(mute_end_timestamp, datetime):
            mute_end_timestamp = int(mute_end_timestamp.timestamp())
        if isinstance(mute_seconds, timedelta):
            mute_seconds = int(mute_seconds.total_seconds())
        return await self._request(
            "PATCH",
            api_path("guilds", guild_id, "mute"),
            json=_payload(
                {"user_ids": user_ids, "mute_end_timestamp": str(mute_end_timestamp), "mute_seconds": str(mute_seconds)}
            ),
        )

    async def post_guild_announces(
        self,
        *,
        guild_id: str,
        message_id: str | None = None,
        channel_id: str | None = None,
        announces_type: int | None = None,
        recommend_channels: list[RecommendChannel] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("guilds", guild_id, "announces"),
            json=_payload(
                {
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "announces_type": announces_type,
                    "recommend_channels": [_payload(r) for r in recommend_channels]
                    if recommend_channels is not None
                    else None,
                }
            ),
        )

    async def delete_guild_announces(self, *, guild_id: str, message_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("guilds", guild_id, "announces", message_id))

    async def put_pins_message(self, *, channel_id: str, message_id: str) -> dict[str, Any]:
        return await self._request("PUT", api_path("channels", channel_id, "pins", message_id))

    async def delete_pins_message(self, *, channel_id: str, message_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("channels", channel_id, "pins", message_id))

    async def get_pins_message(self, *, channel_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("channels", channel_id, "pins"))

    async def get_schedules(self, *, channel_id: str, since: int | datetime | None = None) -> list[dict[str, Any]]:
        if isinstance(since, datetime):
            since = int(since.timestamp() * 1000)
        return await self._request("GET", api_path(f"channels/{channel_id}/schedules"), json=_payload({"since": since}))

    async def get_schedule(self, *, channel_id: str, schedule_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("channels", channel_id, "schedules", schedule_id))

    async def post_schedule(
        self,
        *,
        channel_id: str,
        name: str,
        description: str | None = None,
        start_timestamp: int | datetime,
        end_timestamp: int | datetime,
        jump_channel_id: str | None = None,
        remind_type: RemindType | int,
    ) -> dict[str, Any]:
        if isinstance(start_timestamp, datetime):
            start_timestamp = int(start_timestamp.timestamp() * 1000)
        if isinstance(end_timestamp, datetime):
            end_timestamp = int(end_timestamp.timestamp() * 1000)
        return await self._request(
            "POST",
            api_path("channels", channel_id, "schedules"),
            json={
                "schedule": _payload(
                    {
                        "name": name,
                        "description": description,
                        "start_timestamp": str(start_timestamp) if start_timestamp is not None else None,
                        "end_timestamp": str(end_timestamp) if end_timestamp is not None else None,
                        "jump_channel_id": jump_channel_id,
                        "remind_type": str(remind_type),
                    }
                )
            },
        )

    async def patch_schedule(
        self,
        *,
        channel_id: str,
        schedule_id: str,
        name: str | None = None,
        description: str | None = None,
        start_timestamp: int | datetime | None = None,
        end_timestamp: int | datetime | None = None,
        jump_channel_id: int | None = None,
        remind_type: RemindType | int | None = None,
    ) -> dict[str, Any]:
        if isinstance(start_timestamp, datetime):
            start_timestamp = int(start_timestamp.timestamp() * 1000)
        if isinstance(end_timestamp, datetime):
            end_timestamp = int(end_timestamp.timestamp() * 1000)
        return await self._request(
            "PATCH",
            api_path("channels", channel_id, "schedules", schedule_id),
            json={
                "schedule": _payload(
                    {
                        "name": name,
                        "description": description,
                        "start_timestamp": str(start_timestamp) if start_timestamp is not None else None,
                        "end_timestamp": str(end_timestamp) if end_timestamp is not None else None,
                        "jump_channel_id": jump_channel_id,
                        "remind_type": str(remind_type) if remind_type is not None else None,
                    }
                )
            },
        )

    async def delete_schedule(self, *, channel_id: str, schedule_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("channels", channel_id, "schedules", schedule_id))

    async def put_message_reaction(
        self, *, channel_id: str, message_id: str, type: EmojiType | int, id: str
    ) -> dict[str, Any]:
        return await self._request(
            "PUT", api_path("channels", channel_id, "messages", message_id, "reactions", str(type), id)
        )

    async def delete_own_message_reaction(
        self, *, channel_id: str, message_id: str, type: EmojiType | int, id: str
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE", api_path("channels", channel_id, "messages", message_id, "reactions", str(type), id)
        )

    async def get_reaction_users(
        self,
        *,
        channel_id: str,
        message_id: str,
        type: EmojiType | int,
        id: str,
        cookie: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            api_path("channels", channel_id, "messages", message_id, "reactions", str(type), id),
            params=_payload({"cookie": cookie, "limit": limit}),
        )

    async def audio_control(
        self, *, channel_id: str, audio_url: str | None = None, text: str | None = None, status: AudioStatus | int
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("channels", channel_id, "audio"),
            json=_payload(AudioControl(audio_url=audio_url, text=text, status=status)),
        )

    async def put_mic(self, *, channel_id: str) -> dict[str, Any]:
        return await self._request("PUT", api_path("channels", channel_id, "mic"))

    async def delete_mic(self, *, channel_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("channels", channel_id, "mic"))

    async def get_threads_list(self, *, channel_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("channels", channel_id, "threads"))

    async def get_thread(self, *, channel_id: str, thread_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("channels", channel_id, "threads", thread_id))

    async def put_thread(
        self, *, channel_id: str, title: str, content: str | RichText, format: Literal[1, 2, 3, 4]
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            api_path("channels", channel_id, "threads"),
            json=_payload(
                {
                    "title": title,
                    "content": json.dumps(_payload(content)) if isinstance(content, RichText) else content,
                    "format": format,
                }
            ),
        )

    async def delete_thread(self, *, channel_id: str, thread_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("channels", channel_id, "threads", thread_id))

    async def get_guild_api_permission(self, *, guild_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("guilds", guild_id, "api_permission"))

    async def post_api_permission_demand(
        self, *, guild_id: str, channel_id: str, api_identify: APIPermissionDemandIdentify, desc: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("guilds", guild_id, "api_permission", "demand"),
            json=_payload({"channel_id": channel_id, "api_identify": _payload(api_identify), "desc": desc}),
        )

    async def url_get(self) -> dict[str, Any]:
        return await self._request("GET", api_path("gateway"))

    async def shard_url_get(self) -> dict[str, Any]:
        return await self._request("GET", api_path("gateway", "bot"))

    async def put_interaction(self, *, interaction_id: str, code: Literal[0, 1, 2, 3, 4, 5]) -> dict[str, Any]:
        return await self._request("PUT", api_path("interactions", interaction_id), json={"code": code})

    async def post_c2c_messages(
        self,
        *,
        openid: str,
        msg_type: Literal[0, 1, 2, 3, 4, 7],
        content: str | None = None,
        markdown: MessageMarkdown | None = None,
        keyboard: MessageKeyboard | None = None,
        media: Media | None = None,
        ark: MessageArk | None = None,
        embed: MessageEmbed | None = None,
        image: None = None,
        message_reference: MessageReference | None = None,
        stream: MessageStream | None = None,
        prompt_keyboard: MessagePromptKeyboard | None = None,
        action_button: MessageActionButton | None = None,
        event_id: str | None = None,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        timestamp: int | datetime | None = None,
    ) -> dict[str, Any]:
        if isinstance(timestamp, datetime):
            timestamp = int(timestamp.timestamp())
        elif timestamp is None:
            timestamp = int(datetime.now(timezone.utc).timestamp())
        json_data = _payload(
            {
                "msg_type": msg_type,
                "content": content,
                "markdown": _payload(markdown) if markdown is not None else None,
                "keyboard": _payload(keyboard) if keyboard is not None else None,
                "media": _payload(media) if media is not None else None,
                "ark": _payload(ark) if ark is not None else None,
                "embed": _payload(embed) if embed is not None else None,
                "image": image,
                "message_reference": _payload(message_reference) if message_reference is not None else None,
                "stream": _payload(stream, exclude_unset=True) if stream is not None else None,
                "prompt_keyboard": _payload(prompt_keyboard, exclude_unset=True)
                if prompt_keyboard is not None
                else None,
                "action_button": _payload(action_button) if action_button is not None else None,
                "event_id": event_id,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
                "timestamp": timestamp,
            }
        )
        return await self._request("POST", api_path("v2", "users", openid, "messages"), json=json_data)

    async def post_c2c_files(
        self,
        *,
        openid: str,
        file_type: Literal[1, 2, 3, 4] | None = None,
        url: str | None = None,
        srv_send_msg: bool = True,
        file_data: str | bytes | None = None,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(file_data, bytes):
            file_data = b64encode(file_data).decode()
        if upload_id is not None:
            data = {"upload_id": upload_id}
        elif file_type is None:
            raise ValueError("file_type must be provided if upload_id is not provided")
        else:
            data = _payload({"file_type": file_type, "url": url, "srv_send_msg": srv_send_msg, "file_data": file_data})
        return await self._request("POST", api_path("v2", "users", openid, "files"), json=data)

    async def post_c2c_upload_prepare(
        self,
        *,
        openid: str,
        file_type: Literal[1, 2, 3, 4],
        file_name: str,
        file_size: int,
        md5: str,
        sha1: str,
        md5_10m: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "users", openid, "upload_prepare"),
            json={
                "file_type": file_type,
                "file_name": file_name,
                "file_size": file_size,
                "md5": md5,
                "sha1": sha1,
                "md5_10m": md5_10m,
            },
        )

    async def post_c2c_upload_part_finish(
        self, *, openid: str, upload_id: str, part_index: int, block_size: int, md5: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "users", openid, "upload_part_finish"),
            json={"upload_id": upload_id, "part_index": part_index, "block_size": block_size, "md5": md5},
        )

    async def delete_c2c_message(self, *, openid: str, message_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("v2", "users", openid, "messages", message_id))

    async def post_group_messages(
        self,
        *,
        group_openid: str,
        msg_type: Literal[0, 1, 2, 3, 4, 7],
        content: str | None = None,
        markdown: MessageMarkdown | None = None,
        keyboard: MessageKeyboard | None = None,
        media: Media | None = None,
        ark: MessageArk | None = None,
        embed: MessageEmbed | None = None,
        image: None = None,
        message_reference: MessageReference | None = None,
        event_id: str | None = None,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        timestamp: int | datetime | None = None,
    ) -> dict[str, Any]:
        if isinstance(timestamp, datetime):
            timestamp = int(timestamp.timestamp())
        elif timestamp is None:
            timestamp = int(datetime.now(timezone.utc).timestamp())
        return await self._request(
            "POST",
            api_path("v2", "groups", group_openid, "messages"),
            json=_payload(
                {
                    "msg_type": msg_type,
                    "content": content,
                    "markdown": _payload(markdown) if markdown is not None else None,
                    "keyboard": _payload(keyboard) if keyboard is not None else None,
                    "media": _payload(media) if media is not None else None,
                    "ark": _payload(ark) if ark is not None else None,
                    "embed": _payload(embed) if embed is not None else None,
                    "image": image,
                    "message_reference": _payload(message_reference) if message_reference is not None else None,
                    "event_id": event_id,
                    "msg_id": msg_id,
                    "msg_seq": msg_seq,
                    "timestamp": timestamp,
                }
            ),
        )

    async def post_group_files(
        self,
        *,
        group_openid: str,
        file_type: Literal[1, 2, 3, 4] | None = None,
        url: str | None = None,
        srv_send_msg: bool = True,
        file_data: str | bytes | None = None,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(file_data, bytes):
            file_data = b64encode(file_data).decode()
        if upload_id is not None:
            data = {"upload_id": upload_id}
        elif file_type is None:
            raise ValueError("file_type must be provided if upload_id is not provided")
        else:
            data = _payload({"file_type": file_type, "url": url, "srv_send_msg": srv_send_msg, "file_data": file_data})
        return await self._request("POST", api_path("v2", "groups", group_openid, "files"), json=data)

    async def post_group_upload_prepare(
        self,
        *,
        group_openid: str,
        file_type: Literal[1, 2, 3, 4],
        file_name: str,
        file_size: int,
        md5: str,
        sha1: str,
        md5_10m: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "groups", group_openid, "upload_prepare"),
            json={
                "file_type": file_type,
                "file_name": file_name,
                "file_size": file_size,
                "md5": md5,
                "sha1": sha1,
                "md5_10m": md5_10m,
            },
        )

    async def post_group_upload_part_finish(
        self, group_openid: str, upload_id: str, part_index: int, block_size: int, md5: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "groups", group_openid, "upload_part_finish"),
            json={"upload_id": upload_id, "part_index": part_index, "block_size": block_size, "md5": md5},
        )

    async def delete_group_message(self, *, group_openid: str, message_id: str) -> dict[str, Any]:
        return await self._request("DELETE", api_path("v2", "groups", group_openid, "messages", message_id))

    async def post_group_members(
        self, *, group_id: str, limit: int | None = None, start_index: int | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "groups", group_id, "members"),
            json=_payload({"limit": limit, "start_index": start_index}),
        )

    async def get_group_info(self, *, group_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("v2", "groups", group_id, "info"))

    async def get_group_bot_state(self, *, group_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("v2", "groups", group_id, "bot_state"))

    async def get_group_mute_setting(self, *, group_id: str) -> dict[str, Any]:
        return await self._request("GET", api_path("v2", "groups", group_id, "restrict_chat_setting"))

    async def set_group_members_mute(self, *, group_id: str, members: list[SetMemberMuteState]) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "groups", group_id, "restrict_chat_setting"),
            json={"members": [_payload(m) for m in members]},
        )

    async def get_group_join_request_list(
        self, *, group_id: str, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            api_path("v2", "groups", group_id, "join_request_list"),
            params=_payload({"cursor": cursor, "limit": limit}),
        )

    async def approval_join_request(
        self,
        *,
        group_id: str,
        member_openid: str,
        op: Literal["approve", "decline"],
        join_request_id: str | None = None,
        reject_reason: str | None = None,
        add_to_member_blacklist: bool | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "groups", group_id, "approval_join_request", member_openid),
            json=_payload(
                {
                    "op": op,
                    "join_request_id": join_request_id,
                    "reject_reason": reject_reason,
                    "add_to_member_blacklist": add_to_member_blacklist,
                }
            ),
        )

    async def get_group_members(self, *, group_openid: str, cursor: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET", api_path("v2", "groups", group_openid, "members"), params=_payload({"cursor": cursor})
        )

    async def get_group_member(self, *, group_openid: str, member_openid: str) -> dict[str, Any]:
        return await self._request("GET", api_path("v2", "groups", group_openid, "members", member_openid))

    async def batch_remove_group_members(
        self, *, group_openid: str, member_openids: list[str], add_to_member_blacklist: bool | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "groups", group_openid, "batch_remove_members"),
            json=_payload({"member_openids": member_openids, "add_to_member_blacklist": add_to_member_blacklist}),
        )

    async def get_group_member_blacklist(
        self, *, group_openid: str, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            api_path("v2", "groups", group_openid, "member_blacklist"),
            params=_payload({"cursor": cursor, "limit": limit}),
        )

    async def post_group_member_blacklist(
        self, *, group_openid: str, op: Literal["add", "del"], member_openids: list[str]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            api_path("v2", "groups", group_openid, "member_blacklist"),
            json=_payload({"op": op, "member_openids": member_openids}),
        )


QQ_API = CapabilityKey[QQActions]("qq.api")
