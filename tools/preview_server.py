"""隔离预览适配器：同一份生产视图在不带 same-origin 权限的 iframe 中运行。"""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from github_bridge.panel import PanelAPI

_current = contextvars.ContextVar("preview_request")


class Query:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None, type=None):
        value = self.values.get(key, default)
        if type is None:
            return value
        try:
            return type(value)
        except (ValueError, TypeError):
            return default


class Request:
    def __init__(self, source):
        self.source = source
        self.username = "local-preview"
        self.query = Query(source.query)

    async def body(self):
        return await self.source.read()

    async def json(self, default=None):
        try:
            return await self.source.json()
        except (ValueError, UnicodeError):
            return default


class RequestProxy:
    def __getattr__(self, name):
        return getattr(_current.get(), name)


WEB_API = SimpleNamespace(
    request=RequestProxy(),
    json_response=lambda data, status_code=200: web.json_response(
        data, status=status_code
    ),
    error_response=lambda message, status_code=400: web.json_response(
        {"error": message}, status=status_code
    ),
)

BRIDGE = """(() => {
  let count = 0;
  const pending = new Map();
  window.addEventListener('message', event => {
    if (event.source !== parent || event.data?.channel !== 'bridge-preview') return;
    const entry = pending.get(event.data.id);
    if (!entry) return;
    pending.delete(event.data.id);
    event.data.ok ? entry.resolve(event.data.data) : entry.reject(new Error(event.data.error));
  });
  const call = (method, endpoint, data) => new Promise((resolve, reject) => {
    const id = ++count;
    pending.set(id, {resolve, reject});
    parent.postMessage({channel:'bridge-preview',id,method,endpoint,data}, '*');
  });
  window.AstrBotPluginView = {
    ready: async () => ({pluginName:'astrbot_plugin_github_bridge',isDark:false}),
    getContext: () => ({isDark:false}),
    onContext: () => () => {},
    apiGet: (endpoint, params) => call('GET', endpoint, params),
    apiPost: (endpoint, data) => call('POST', endpoint, data)
  };
})();"""

HOST = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>GitHub Bridge · Dashboard iframe 预览</title>
<style>html,body,iframe{margin:0;width:100%;height:100%;border:0;display:block}</style>
</head><body><iframe src="/view/" title="GitHub Bridge" sandbox="allow-scripts allow-forms allow-downloads"></iframe>
<script>
window.addEventListener('message', async event => {
  const frame = document.querySelector('iframe');
  const msg = event.data;
  if (event.source !== frame.contentWindow || msg?.channel !== 'bridge-preview') return;
  if (!/^[a-z_]+$/.test(msg.endpoint) || !['GET','POST'].includes(msg.method)) return;
  try {
    let url = '/api/' + msg.endpoint;
    const init = {method:msg.method,headers:{'Content-Type':'application/json'}};
    if (msg.method === 'GET') url += '?' + new URLSearchParams(msg.data || {});
    else init.body = JSON.stringify(msg.data || {});
    const response = await fetch(url, init);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || data.message || '请求失败');
    event.source.postMessage({channel:'bridge-preview',id:msg.id,ok:true,data}, '*');
  } catch (error) {
    event.source.postMessage({channel:'bridge-preview',id:msg.id,ok:false,error:error.message}, '*');
  }
});
</script></body></html>"""


class PreviewServer:
    def __init__(self, service, view_dir: Path, event_delay: float = 0):
        self.view_dir = view_dir
        self.event_delay = event_delay
        self.api = PanelAPI(service, web_api=WEB_API)
        self.runner = None
        self.app = web.Application(client_max_size=2 * 1024 * 1024)
        self.app.add_routes(
            [
                web.get("/", self.host),
                web.get("/view/", self.index),
                web.get("/view/style.css", self.asset),
                web.get("/view/app.js", self.asset),
                web.get("/view/issues.js", self.asset),
                web.get("/bridge.js", self.bridge),
                web.route("*", "/api/{endpoint}", self.dispatch),
            ]
        )

    async def dispatch(self, source):
        marker = _current.set(Request(source))
        try:
            response = await self.api.dispatch(
                source.method, source.match_info["endpoint"]
            )
        finally:
            _current.reset(marker)
        if (
            source.match_info["endpoint"] == "events"
            and source.query.get("status") == "all"
            and self.event_delay
        ):
            await asyncio.sleep(self.event_delay)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def host(self, _):
        return web.Response(text=HOST, content_type="text/html")

    async def index(self, _):
        html = (self.view_dir / "index.html").read_text(encoding="utf-8")
        html = html.replace("</head>", '<script src="/bridge.js"></script></head>')
        return web.Response(text=html, content_type="text/html")

    async def asset(self, source):
        return web.FileResponse(
            self.view_dir / source.path.rsplit("/", 1)[-1],
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def bridge(self, _):
        return web.Response(
            text=BRIDGE,
            content_type="application/javascript",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def start(self, host, port):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, host, port).start()

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
