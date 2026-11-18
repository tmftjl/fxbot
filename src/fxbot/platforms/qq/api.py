"""QQ 官方 HTTP 鉴权和单次请求，不在写操作失败后自动重试。"""

from __future__ import annotations

import json as json_module
import math
import time
import asyncio
from urllib.parse import urlsplit

import httpx

from fxbot.core.errors import BotOffline, ActionFailed

API_BASE = "https://api.bot.qq.com"
AUTH_URL = "https://api.bot.qq.com/app/getAppAccessToken"
# 与原版 HTTP 驱动一致：上传不设写超时，只限制等待响应的时间。
UPLOAD_READ_TIMEOUT = 30.0


class QQAPIError(ActionFailed):
    def __init__(self, status: int, code: int | None = None, message: str = "", trace_id: str = "") -> None:
        self.status, self.code = status, code
        detail = f"QQ 官方接口请求失败（HTTP {status}，code={code}）"
        if message:
            detail += f"：{message}"
        if trace_id:
            detail += f"（trace_id={trace_id}）"
        super().__init__(detail)


class QQHTTP:
    def __init__(
        self, app_id: str, secret: str, http: httpx.AsyncClient, *, api_base: str = API_BASE, auth_url: str = AUTH_URL
    ) -> None:
        self.app_id, self._secret, self._http = app_id, secret, http
        self._api_base, self._auth_url = api_base.rstrip("/"), auth_url
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._closed = False

    async def token(self) -> str:
        if self._closed:
            raise BotOffline("QQ 客户端已关闭")
        async with self._lock:
            if self._closed:
                raise BotOffline("QQ 客户端已关闭")
            if time.monotonic() < self._expires_at:
                return self._token
            response = await self._http.post(self._auth_url, json={"appId": self.app_id, "clientSecret": self._secret})
            data = self._response(response)
            if self._closed:
                raise BotOffline("QQ 客户端已关闭")
            token, ttl = data.get("access_token"), data.get("expires_in")
            if isinstance(ttl, str) and ttl.isascii() and ttl.isdecimal():
                ttl = int(ttl)
            if (
                not isinstance(token, str)
                or not token
                or isinstance(ttl, bool)
                or not isinstance(ttl, (int, float))
                or not math.isfinite(ttl)
                or ttl <= 0
            ):
                raise ValueError("QQ 鉴权响应缺少有效 token 或有效期")
            self._token = token
            self._expires_at = time.monotonic() + ttl - min(30, ttl / 10)
            return token

    def _redact(self, value: str) -> str:
        """错误详情可能回显请求内容，先抹掉本进程持有的凭证。"""
        for secret in (self._secret, self._token):
            if secret:
                value = value.replace(secret, "***")
        return value[:300]

    def _error_details(self, error: object) -> tuple[int | None, str, str]:
        if not isinstance(error, dict):
            return None, "", ""
        code = error.get("code")
        message = error.get("message") or error.get("msg")
        trace_id = error.get("trace_id") or error.get("traceId")
        return (
            code if type(code) is int else None,
            self._redact(message) if isinstance(message, str) else "",
            self._redact(trace_id) if isinstance(trace_id, str) else "",
        )

    def _response(self, response: httpx.Response) -> dict:
        if not 200 <= response.status_code < 300:
            try:
                error = response.json()
            except (json_module.JSONDecodeError, UnicodeDecodeError):
                error = {}
            raise QQAPIError(response.status_code, *self._error_details(error))
        if not response.content:
            return {}
        try:
            data = response.json()
        except (json_module.JSONDecodeError, UnicodeDecodeError) as error:
            raise QQAPIError(response.status_code) from error
        if not isinstance(data, dict):
            raise QQAPIError(response.status_code)
        if data.get("code", 0) != 0:
            raise QQAPIError(response.status_code, *self._error_details(data))
        return data

    def _upload_timeout(self) -> httpx.Timeout:
        """上传保留连接和读超时，不额外限制请求体写入时间。"""
        base = self._http.timeout
        return httpx.Timeout(base.connect, read=UPLOAD_READ_TIMEOUT, write=None, pool=base.pool)

    async def request(
        self, method: str, path: str, *, json=None, data=None, files=None, params=None, available=None, upload=False
    ) -> dict:
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise ValueError("QQ 接口必须使用固定相对路径")
        token = await self.token()
        if self._closed:
            raise BotOffline("QQ 客户端已关闭")
        if available is not None:
            available()
        options = {"timeout": self._upload_timeout()} if upload else {}
        response = await self._http.request(
            method,
            self._api_base + path,
            headers={"Authorization": f"QQBot {token}", "X-Union-Appid": self.app_id},
            json=json,
            data=data,
            files=files,
            params=params,
            **options,
        )
        if response.status_code == 204:
            return {}
        if response.status_code == 401 and self._token == token:
            self._expires_at = 0
            self._token = ""
        return self._response(response)

    async def put_upload_part(self, url: str, content: bytes, *, available=None) -> None:
        """预签名地址使用独立请求，不携带 Bot 凭证、不跟随重定向。"""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("QQ 预签名上传必须是 HTTPS 地址")
        if self._closed:
            raise BotOffline("QQ 客户端已关闭")
        if available is not None:
            available()
        response = await self._http.put(url, content=content, timeout=self._upload_timeout())
        if not 200 <= response.status_code < 300:
            raise QQAPIError(response.status_code)

    async def close(self) -> None:
        self._closed = True
        self._token = ""
        self._secret = ""
        self._expires_at = 0
        await self._http.aclose()
