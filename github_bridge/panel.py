from __future__ import annotations

import json
from typing import Any

from .service import BridgeService

try:
    from astrbot.api.web import json_response, request
except ImportError:  # 本地开发测试使用轻量适配层。
    from .web_compat import json_response, request


class PanelAPI:
    """把管理页业务接口接到 AstrBot Dashboard 的插件 Web API。"""

    GET_ROUTES = ("session", "status", "repositories", "events", "settings")
    POST_ROUTES = ("settings", "check", "test", "retry", "import", "logout")

    def __init__(self, service: BridgeService | None):
        self.service = service
        self.handlers = {
            ("GET", "session"): self.session,
            ("GET", "status"): self.status,
            ("GET", "repositories"): self.repositories,
            ("GET", "events"): self.events,
            ("GET", "settings"): self.settings,
            ("POST", "settings"): self.save_settings,
            ("POST", "check"): self.check,
            ("POST", "test"): self.test,
            ("POST", "retry"): self.retry,
            ("POST", "import"): self.import_legacy,
            ("POST", "logout"): self.logout,
        }

    async def dispatch(self, method: str, endpoint: str):
        handler = self.handlers.get((method, endpoint.strip("/")))
        if handler is None:
            return json_response({"error": "未找到该接口"}, status_code=404)
        try:
            return await handler()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return json_response({"error": self.service.safe_error(exc)}, status_code=400)
        except Exception as exc:
            return json_response({"error": self.service.safe_error(exc)}, status_code=502)

    async def payload(self) -> dict[str, Any]:
        data = await request.json(default={})
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    async def session(self):
        return json_response({"authenticated": True, "username": request.username})

    async def logout(self):
        return json_response({"ok": False, "error": "请通过 AstrBot Dashboard 退出登录"}, status_code=400)

    async def status(self):
        return json_response({**self.service.status(), "csrf": "dashboard"})

    async def repositories(self):
        return json_response({"items": self.service.store.repositories(self.service.settings.value.identity)})

    async def events(self):
        status = request.query.get("status", "all")
        if status not in {"all", "pending", "sent", "failed", "sending"}:
            raise ValueError("不支持的通知状态")
        limit = request.query.get("limit", 50, type=int)
        return json_response({"items": self.service.store.events(self.service.settings.value.identity, status, max(1, min(200, limit or 50)))})

    async def settings(self):
        return json_response(self.service.settings.value.public())

    async def save_settings(self):
        return json_response(await self.service.update_settings(await self.payload()))

    async def check(self):
        self.service.request_check()
        return json_response({"ok": True, "message": "检查已排队，结果会自动刷新"})

    async def test(self):
        return json_response({"ok": True, **await self.service.test_connection()})

    async def retry(self):
        data = await self.payload()
        event_id = data.get("id")
        if event_id is not None and (type(event_id) is not int or event_id < 1):
            raise ValueError("通知 ID 必须为正整数")
        cfg = self.service.settings.value
        count = self.service.store.retry(cfg.identity, cfg.target_umo, event_id)
        return json_response({"ok": True, "count": count})

    async def import_legacy(self):
        from .migration import parse_legacy
        if self.service.running:
            raise ValueError("请等待当前检查结束再导入旧记录")
        data = await self.payload()
        cfg = self.service.settings.value
        parsed = parse_legacy(data.get("sent"), data.get("failed"), cfg)
        imported, queued = self.service.store.import_records(cfg.identity, cfg.target_umo, parsed.seen, parsed.failed)
        return json_response({"ok": True, "imported": imported, "queued": queued,
                              "skipped": parsed.skipped, "warnings": parsed.warnings})
