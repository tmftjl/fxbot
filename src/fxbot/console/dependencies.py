"""控制台通过请求显式取得当前应用的服务。"""

from __future__ import annotations

from fastapi import Request

from ..bootstrap import Runtime


def runtime_for(request: Request) -> Runtime:
    """不读取全局框架容器。"""
    return request.app.state.services
