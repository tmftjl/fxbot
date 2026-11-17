"""适配器配置：OneBot 访问令牌、监听参数与 QQ 官方 Bot 凭证。"""

from __future__ import annotations

import asyncio
from typing import Any
from hashlib import sha1

from loguru import logger
from fastapi import Depends, Request, APIRouter, HTTPException

from ..auth import bearer_auth
from ...config.storage import ConfigStorage
from ...runtime.settings import Settings

router = APIRouter(prefix="/adapters", tags=["fxbot-adapters"], dependencies=[Depends(bearer_auth)])


def _storage() -> ConfigStorage:
    """运行时配置存储；测试可替换为临时目录。"""
    return ConfigStorage("runtime")


def _revision(storage: ConfigStorage) -> str:
    """按文件内容计算修订号，用于控制台并发保存检测。"""
    try:
        return sha1(storage.path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""


def _load(request: Request) -> tuple[Settings, str, str | None]:
    """优先读取磁盘配置；文件缺失或损坏时回落到当前运行值。"""
    storage = _storage()
    revision = _revision(storage)
    try:
        return Settings.load(storage.path), revision, None
    except FileNotFoundError:
        return request.app.state.settings.current, revision, "运行时配置文件不存在，当前展示的是进程启动时的设置。"
    except (OSError, ValueError) as exc:
        logger.warning("[Console] 读取运行时配置失败: {}", exc)
        return request.app.state.settings.current, revision, "运行时配置文件无法解析，当前展示的是进程运行中的设置。"


def _payload(request: Request, settings: Settings, revision: str, warning: str | None = None) -> dict[str, Any]:
    """返回控制台需要的配置、在线 Bot 和修订号。"""
    return {
        "config": settings.to_payload(),
        "revision": revision,
        "online": [{"adapter": bot.platform, "self_id": bot.bot_id} for bot in request.app.state.runtime.bots],
        "warning": warning,
    }


async def _disconnect_onebot(application) -> list[str]:
    """令牌变更后关闭旧 OneBot 连接，要求客户端用新令牌重新握手。"""
    bots = [bot for bot in application.bots if bot.platform == "onebot11" or bot.platform.startswith("onebot12.")]
    results = await asyncio.gather(*(application.disconnect(bot) for bot in bots), return_exceptions=True)
    failed = []
    for bot, result in zip(bots, results, strict=True):
        if isinstance(result, BaseException):
            logger.opt(exception=result).error("[Console] 断开旧 OneBot 连接失败")
            failed.append(f"{bot.platform}:{bot.bot_id}")
    return failed


@router.get("")
async def get_adapters(request: Request) -> dict[str, Any]:
    """读取当前运行时配置和在线 Bot。"""
    settings, revision, warning = _load(request)
    return _payload(request, settings, revision, warning)


@router.put("")
async def update_adapters(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """校验并保存运行时配置；令牌和 QQ 凭证立即生效，监听地址待重启。"""
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(400, "配置必须是对象")
    storage = _storage()
    async with request.app.state.adapter_lock:
        revision = _revision(storage)
        if payload.get("base_revision") != revision:
            raise HTTPException(409, "配置已被修改，请刷新后重试")
        try:
            candidate = Settings.from_mapping(config)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        previous = request.app.state.settings.current
        try:
            storage.write(config)
        except OSError as exc:
            logger.opt(exception=True).error("[Console] 保存运行时配置失败")
            raise HTTPException(500, "保存失败，请稍后再试") from exc
        request.app.state.settings.replace(candidate)
        warnings = []
        if dict(previous.qq_bots) != dict(candidate.qq_bots):
            try:
                await request.app.state.services.qq.reload(candidate)
            except Exception:
                logger.opt(exception=True).error("[Console] 重载 QQ Bot 连接失败")
                warnings.append("QQ Bot 配置已保存，但连接重载失败，请重启 FxBot 生效。")
        if previous.token != candidate.token:
            failed = await _disconnect_onebot(request.app.state.runtime)
            if failed:
                warnings.append(f"令牌已保存，但以下 OneBot 连接未能主动断开：{', '.join(failed)}")
        return _payload(request, candidate, _revision(storage), "；".join(warnings) or None)
