from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .panel import PanelAPI
from .web_compat import Request, set_request

HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer", "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; frame-ancestors 'self'",
}


class PreviewServer:
    """本地界面预览；生产页面由 AstrBot Dashboard 的 views/ 机制承载。"""

    def __init__(self, service, web_dir: Path):
        self.service = service
        self.web_dir = web_dir
        self.api = PanelAPI(service)
        self.runner: web.AppRunner | None = None
        self.app = web.Application(client_max_size=2 * 1024 * 1024)
        self.app.add_routes([
            web.get("/", self.index), web.get("/style.css", self.asset), web.get("/app.js", self.asset),
            web.route("*", "/api/{tail:.*}", self.dispatch),
        ])

    async def dispatch(self, aio_request: web.Request):
        set_request(Request(aio_request))
        try:
            response = await self.api.dispatch(aio_request.method, aio_request.match_info["tail"])
        finally:
            set_request(None)
        response.headers.update(HEADERS)
        return response

    async def index(self, request: web.Request):
        return web.FileResponse(self.web_dir / "index.html")

    async def asset(self, request: web.Request):
        return web.FileResponse(self.web_dir / request.path.lstrip("/"))

    async def start(self, host: str, port: int):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, host, port).start()

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
