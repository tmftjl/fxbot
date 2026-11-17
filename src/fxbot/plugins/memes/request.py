"""meme-generator 服务请求与数据模型；参数展示使用标准库实现。"""

from __future__ import annotations

import json
from typing import Any, Union, Literal, Optional, cast, overload
from datetime import datetime
from dataclasses import field, dataclass

import httpx

from .config import cfg_base_url
from .exception import (
    NoSuchMeme,
    ArgMismatch,
    MemeFeedback,
    ParamsMismatch,
    TextOverLength,
    OpenImageFailed,
    ArgModelMismatch,
    ArgParserMismatch,
    TextNumberMismatch,
    ImageNumberMismatch,
    TextOrNameNotEnough,
    MemeGeneratorException,
)


def _base_url() -> str:
    """读取表情生成服务地址。"""
    return cfg_base_url()


@overload
async def send_request(
    router: str,
    request_type: Literal["POST", "GET"],
    response_type: Literal["JSON"],
    **kwargs,
) -> Union[dict[str, Any], list[Any]]: ...


@overload
async def send_request(
    router: str,
    request_type: Literal["POST", "GET"],
    response_type: Literal["BYTES"],
    **kwargs,
) -> bytes: ...


@overload
async def send_request(
    router: str,
    request_type: Literal["POST", "GET"],
    response_type: Literal["TEXT"],
    **kwargs,
) -> str: ...


async def send_request(
    router: str,
    request_type: Literal["POST", "GET"],
    response_type: Literal["JSON", "BYTES", "TEXT"],
    **kwargs,
):
    """发送一次服务请求，并按旧版约定的状态码转换为业务异常。"""
    async with httpx.AsyncClient(timeout=300) as client:
        request_method = client.post if request_type == "POST" else client.get
        resp = await request_method(_base_url() + router, **kwargs)
        status_code = resp.status_code
        if status_code == 200:
            if response_type == "JSON":
                return resp.json()
            if response_type == "BYTES":
                return resp.content
            return resp.text
        if 520 <= status_code < 600:
            message = resp.json()["detail"]
            if 560 <= status_code < 570:
                raise MemeFeedback(message)
            if status_code == 551:
                raise ArgParserMismatch(message)
            if status_code == 552:
                raise ArgModelMismatch(message)
            if 550 <= status_code < 560:
                raise ArgMismatch(message)
            if status_code == 541:
                raise ImageNumberMismatch(message)
            if status_code == 542:
                raise TextNumberMismatch(message)
            if status_code == 543:
                raise TextOrNameNotEnough(message)
            if 540 <= status_code < 550:
                raise ParamsMismatch(message)
            if status_code == 531:
                raise NoSuchMeme(message)
            if status_code == 532:
                raise TextOverLength(message)
            if status_code == 533:
                raise OpenImageFailed(message)
            raise MemeGeneratorException(message)
        resp.raise_for_status()


@dataclass(slots=True)
class MemeKeyWithProperties:
    meme_key: str
    disabled: bool = False
    labels: list[Literal["new", "hot"]] = field(default_factory=list)


@dataclass(slots=True)
class RenderMemeListRequest:
    meme_list: list[MemeKeyWithProperties]
    text_template: str = "{keywords}"
    add_category_icon: bool = True


async def render_meme_list(
    meme_list: list[MemeKeyWithProperties],
    *,
    text_template: str = "{keywords}",
    add_category_icon: bool = True,
) -> bytes:
    request = RenderMemeListRequest(meme_list, text_template, add_category_icon)
    payload = {
        "meme_list": [
            {"meme_key": item.meme_key, "disabled": item.disabled, "labels": list(item.labels)}
            for item in request.meme_list
        ],
        "text_template": request.text_template,
        "add_category_icon": request.add_category_icon,
    }
    return await send_request("/memes/render_list", "POST", "BYTES", json=payload)


async def get_meme_keys() -> list[str]:
    return cast(list[str], await send_request("/memes/keys", "GET", "JSON"))


@dataclass(slots=True)
class ParserArg:
    name: str
    value: str
    default: Optional[Any] = None
    flags: Optional[list[Any]] = None


@dataclass(slots=True)
class ParserOption:
    names: list[str]
    args: Optional[list[ParserArg]] = None
    dest: Optional[str] = None
    default: Optional[Any] = None
    action: Optional[Any] = None
    help_text: Optional[str] = None
    compact: bool = False

    def display(self) -> str:
        """按旧参数选项的展示规则生成帮助文本。"""
        required = " ".join(arg.name for arg in self.args or [] if not arg.default)
        aliases = "│".join(sorted(self.names, key=len))
        prefix = required + " " if required else ""
        return f"{prefix}{aliases}={self.help_text}"


@dataclass(slots=True)
class CommandShortcut:
    key: str
    args: Optional[list[str]] = None
    humanized: Optional[str] = None


@dataclass(slots=True)
class MemeArgsType:
    args_model: dict[str, Any]
    args_examples: list[dict[str, Any]]
    parser_options: list[ParserOption]


@dataclass(slots=True)
class MemeParamsType:
    min_images: int
    max_images: int
    min_texts: int
    max_texts: int
    default_texts: list[str]
    args_type: Optional[MemeArgsType] = None


@dataclass(slots=True)
class MemeInfo:
    key: str
    params_type: MemeParamsType
    keywords: list[str]
    shortcuts: list[CommandShortcut]
    tags: set[str]
    date_created: datetime
    date_modified: datetime


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    return value


def _strings(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} 必须是字符串数组")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    return value


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是时间字符串")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} 不是有效时间") from exc


def _parser_arg(raw: Any) -> ParserArg:
    data = _mapping(raw, "ParserArg")
    return ParserArg(
        name=str(data["name"]),
        value=str(data["value"]),
        default=data.get("default"),
        flags=data.get("flags"),
    )


def _parser_option(raw: Any) -> ParserOption:
    data = _mapping(raw, "ParserOption")
    return ParserOption(
        names=_strings(data.get("names"), "ParserOption.names"),
        args=[_parser_arg(item) for item in data.get("args") or []],
        dest=data.get("dest"),
        default=data.get("default"),
        action=data.get("action"),
        help_text=data.get("help_text"),
        compact=bool(data.get("compact", False)),
    )


def _args_type(raw: Any) -> MemeArgsType:
    data = _mapping(raw, "MemeArgsType")
    return MemeArgsType(
        args_model=_mapping(data.get("args_model", {}), "MemeArgsType.args_model"),
        args_examples=[_mapping(item, "MemeArgsType.args_examples") for item in data.get("args_examples") or []],
        parser_options=[_parser_option(item) for item in data.get("parser_options") or []],
    )


def _params_type(raw: Any) -> MemeParamsType:
    data = _mapping(raw, "MemeParamsType")
    args_type = data.get("args_type")
    return MemeParamsType(
        min_images=_integer(data.get("min_images"), "min_images"),
        max_images=_integer(data.get("max_images"), "max_images"),
        min_texts=_integer(data.get("min_texts"), "min_texts"),
        max_texts=_integer(data.get("max_texts"), "max_texts"),
        default_texts=_strings(data.get("default_texts"), "default_texts"),
        args_type=_args_type(args_type) if args_type is not None else None,
    )


def _shortcut(raw: Any) -> CommandShortcut:
    data = _mapping(raw, "CommandShortcut")
    return CommandShortcut(
        key=str(data["key"]),
        args=_strings(data.get("args"), "CommandShortcut.args") if data.get("args") is not None else None,
        humanized=data.get("humanized"),
    )


def meme_info_from_payload(payload: Any) -> MemeInfo:
    """把服务返回的 JSON 转换为表情信息。"""
    data = _mapping(payload, "MemeInfo")
    return MemeInfo(
        key=str(data["key"]),
        params_type=_params_type(data.get("params_type")),
        keywords=_strings(data.get("keywords"), "keywords"),
        shortcuts=[_shortcut(item) for item in data.get("shortcuts") or []],
        tags=set(_strings(data.get("tags"), "tags")),
        date_created=_datetime(data.get("date_created"), "date_created"),
        date_modified=_datetime(data.get("date_modified"), "date_modified"),
    )


async def get_meme_info(meme_key: str) -> MemeInfo:
    return meme_info_from_payload(await send_request(f"/memes/{meme_key}/info", "GET", "JSON"))


async def generate_meme_preview(meme_key: str) -> bytes:
    return await send_request(f"/memes/{meme_key}/preview", "GET", "BYTES")


async def generate_meme(meme_key: str, images: list[bytes], texts: list[str], args: dict[str, Any]) -> bytes:
    files = [("images", image) for image in images]
    data = {"texts": texts, "args": json.dumps(args)}
    return await send_request(f"/memes/{meme_key}/", "POST", "BYTES", files=files, data=data)
