from __future__ import annotations

import json
from typing import Any

from .service import BridgeService
from .workbench import IssueWorkbench

MAX_BODY_BYTES = 2 * 1024 * 1024


class PanelAPI:
    """Dashboard 负责鉴权；这里只处理插件输入和业务响应。"""

    GET_ROUTES = (
        "status",
        "repositories",
        "events",
        "settings",
        "issue_repositories",
        "issues",
        "issue_detail",
    )
    POST_ROUTES = (
        "settings",
        "check",
        "test",
        "retry",
        "import",
        "issue_prepare",
        "issue_confirm",
    )

    def __init__(self, service: BridgeService, web_api=None):
        if web_api is None:
            from astrbot.api import web as web_api
        self.web = web_api
        self.service = service
        self.workbench = IssueWorkbench(service)
        self.handlers = {
            ("GET", "status"): self.status,
            ("GET", "repositories"): self.repositories,
            ("GET", "events"): self.events,
            ("GET", "settings"): self.settings,
            ("POST", "settings"): self.save_settings,
            ("POST", "check"): self.check,
            ("POST", "test"): self.test,
            ("POST", "retry"): self.retry,
            ("POST", "import"): self.import_legacy,
            ("GET", "issue_repositories"): self.issue_repositories,
            ("GET", "issues"): self.issues,
            ("GET", "issue_detail"): self.issue_detail,
            ("POST", "issue_prepare"): self.issue_prepare,
            ("POST", "issue_confirm"): self.issue_confirm,
        }

    async def dispatch(self, method: str, endpoint: str):
        if not self.web.request.username:
            return self.web.error_response(
                "请先登录 AstrBot Dashboard", status_code=401
            )
        handler = self.handlers.get((method, endpoint.strip("/")))
        if handler is None:
            return self.web.json_response({"error": "未找到该接口"}, status_code=404)
        try:
            return await handler()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.web.json_response(
                {"error": self.service.safe_error(exc)}, status_code=400
            )
        except Exception as exc:
            return self.web.json_response(
                {"error": self.service.safe_error(exc)}, status_code=502
            )

    async def payload(self) -> dict[str, Any]:
        raw = await self.web.request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("请求内容不能超过 2 MiB")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("请求必须是有效的 JSON 对象") from None
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    async def status(self):
        return self.web.json_response(self.service.status())

    async def repositories(self):
        return self.web.json_response(
            {
                "items": self.service.store.repositories(
                    self.service.settings.value.identity
                )
            }
        )

    async def events(self):
        status = self.web.request.query.get("status", "all")
        if status not in {"all", "pending", "sent", "failed", "sending"}:
            raise ValueError("不支持的通知状态")
        limit = self.web.request.query.get("limit", 50, type=int)
        return self.web.json_response(
            {
                "items": self.service.store.events(
                    self.service.settings.value.identity,
                    status,
                    max(1, min(200, limit or 50)),
                )
            }
        )

    async def settings(self):
        return self.web.json_response(self.service.settings.value.public())

    async def save_settings(self):
        return self.web.json_response(
            await self.service.update_settings(await self.payload())
        )

    async def check(self):
        previous = self.service.last_check
        self.service.request_check()
        return self.web.json_response(
            {
                "ok": True,
                "last_check": previous,
                "message": "检查已排队，结果会自动刷新",
            }
        )

    async def test(self):
        return self.web.json_response(
            {"ok": True, **await self.service.test_connection()}
        )

    async def retry(self):
        data = await self.payload()
        event_id = data.get("id")
        if event_id is not None and (type(event_id) is not int or event_id < 1):
            raise ValueError("通知 ID 必须为正整数")
        cfg = self.service.settings.value
        count = self.service.store.retry(cfg.identity, cfg.target_umo, event_id)
        return self.web.json_response({"ok": True, "count": count})

    async def issue_repositories(self):
        return self.web.json_response(await self.workbench.repositories())

    def query_number(self, key: str, default: str) -> int:
        raw = self.web.request.query.get(key, default)
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
            raise ValueError("页码和 Issue 编号必须为正整数")
        return int(raw)

    async def issues(self):
        return self.web.json_response(
            await self.workbench.list_issues(
                self.web.request.query.get("repo", ""),
                self.web.request.query.get("state", "open"),
                self.query_number("page", "1"),
            )
        )

    async def issue_detail(self):
        return self.web.json_response(
            await self.workbench.detail(
                self.web.request.query.get("repo", ""),
                self.query_number("number", "0"),
            )
        )

    async def issue_prepare(self):
        return self.web.json_response(
            await self.workbench.prepare(
                await self.payload(), self.web.request.username
            )
        )

    async def issue_confirm(self):
        data = await self.payload()
        return self.web.json_response(
            await self.workbench.confirm(
                data.get("draft_id"), self.web.request.username
            )
        )

    async def import_legacy(self):
        from .migration import parse_legacy

        if self.service.running:
            raise ValueError("请等待当前检查结束再导入旧记录")
        data = await self.payload()
        cfg = self.service.settings.value
        if not cfg.target_umo:
            raise ValueError("导入前请先保存管理员私聊目标")
        if not cfg.github_owner:
            raise ValueError("导入前请先保存自己的 GitHub 用户名")
        parsed = parse_legacy(data.get("sent"), data.get("failed"), cfg)
        imported, queued = self.service.store.import_records(
            cfg.identity, cfg.target_umo, parsed.seen, parsed.failed
        )
        return self.web.json_response(
            {
                "ok": True,
                "imported": imported,
                "queued": queued,
                "skipped": parsed.skipped,
                "warnings": parsed.warnings,
            }
        )
