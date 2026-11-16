"""QQ 群与 C2C 官方接口，参数契约对齐 nonebot/adapter-qq（MIT）。

这里只编码协议动作；连接存活检查、鉴权和资源由传输边界负责。
"""

from __future__ import annotations

from base64 import b64encode
from typing import Any, Literal
from datetime import datetime, timezone

from .codec import api_path
from .uploads import upload
from ...core.capabilities import CapabilityKey


def _payload(value):
    if isinstance(value, dict):
        return {key: _payload(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_payload(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("QQ 时间必须包含时区")
        return value.isoformat()
    return value


class QQActions:
    """官方 API 的显式入口，返回未经业务改写的 JSON 对象。"""

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
        return await self._request("GET", "/users/@me")

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
        markdown: dict[str, Any] | None = None,
        keyboard: dict[str, Any] | None = None,
        media: dict[str, Any] | None = None,
        ark: dict[str, Any] | None = None,
        embed: dict[str, Any] | None = None,
        image: None = None,
        message_reference: dict[str, Any] | None = None,
        stream: dict[str, Any] | None = None,
        prompt_keyboard: dict[str, Any] | None = None,
        action_button: dict[str, Any] | None = None,
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
                "stream": _payload(stream) if stream is not None else None,
                "prompt_keyboard": _payload(prompt_keyboard) if prompt_keyboard is not None else None,
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
        markdown: dict[str, Any] | None = None,
        keyboard: dict[str, Any] | None = None,
        media: dict[str, Any] | None = None,
        ark: dict[str, Any] | None = None,
        embed: dict[str, Any] | None = None,
        image: None = None,
        message_reference: dict[str, Any] | None = None,
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

    async def set_group_members_mute(self, *, group_id: str, members: list[dict[str, Any]]) -> dict[str, Any]:
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
