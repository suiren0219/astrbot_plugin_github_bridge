from __future__ import annotations

import json
from typing import Any

from aiohttp import web


class Query:
    def __init__(self, pairs: list[tuple[str, str]]):
        self.pairs = pairs

    def get(self, key: str, default: Any = None, type: Any = None):
        value = next((item for name, item in reversed(self.pairs) if name == key), default)
        if type is None or value is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


class Request:
    def __init__(self, aio_request: web.Request):
        self._request = aio_request
        self.method = aio_request.method
        self.path = aio_request.path
        self.headers = aio_request.headers
        self.cookies = aio_request.cookies
        self.plugin_name = "astrbot_plugin_github_bridge"
        self.username = "local-preview"
        self.query = Query(list(aio_request.query.items()))
        self.body_bytes = b""

    async def body(self) -> bytes:
        if not self.body_bytes:
            self.body_bytes = await self._request.read()
        return self.body_bytes

    async def json(self, default=None):
        raw = await self.body()
        try:
            return json.loads(raw) if raw else default
        except (json.JSONDecodeError, UnicodeDecodeError):
            return default


class ContextVarProxy:
    def __init__(self):
        self.current: Request | None = None

    def __getattr__(self, name: str):
        if self.current is None:
            raise RuntimeError("仅在管理页请求处理期间可访问 request")
        return getattr(self.current, name)


request = ContextVarProxy()


def set_request(value: Request | None):
    request.current = value


def json_response(data: Any = None, *, status_code: int = 200, headers: dict[str, str] | None = None):
    return web.json_response(data, status=status_code, headers=headers)
