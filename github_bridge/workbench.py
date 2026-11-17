"""后台中文 Issue 工作台：只读浏览与一次性确认写入。"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .feedback import redact
from .issues import IssueGateway


@dataclass
class IssueDraft:
    username: str
    preview: dict[str, Any]
    expires_at: float
    configuration: str


class IssueWorkbench:
    def __init__(self, service):
        self.service = service
        self.drafts: dict[str, IssueDraft] = {}

    def signature(self) -> str:
        cfg = self.service.settings.value
        return hashlib.sha256(
            json.dumps(
                {
                    "owner": cfg.identity,
                    "token": cfg.github_token,
                    "allowed": cfg.issue_repositories,
                    "excluded": cfg.excluded_repositories,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def ensure_active(self):
        if getattr(self.service, "_closed", False) is True:
            raise ValueError("插件正在关闭，请重载后重新操作")

    def client(self):
        self.ensure_active()
        cfg = self.service.settings.value
        if not cfg.github_owner or not cfg.github_token:
            raise ValueError("请先在设置中保存 GitHub 用户名和 Token")
        if time.time() < self.service.cooldown_until:
            raise ValueError("GitHub 请求正在退避，请稍后再试")
        return self.service.client_factory(cfg)

    async def run(self, operation):
        client = self.client()
        try:
            return await operation(client)
        except Exception as exc:
            self.service._rate_error(exc)
            raise ValueError(self.service.safe_error(exc)) from None
        finally:
            await client.close()

    async def repositories(self):
        async def read(client):
            repos = await client.list_repositories()
            gateway = IssueGateway(client)
            fields = (
                "full_name",
                "description",
                "private",
                "archived",
                "has_issues",
                "open_issues_count",
                "html_url",
            )
            return {
                "items": [
                    {
                        **{key: repo.get(key) for key in fields},
                        "can_write": gateway.can_write(repo),
                    }
                    for repo in repos
                ],
                "owner": client.settings.github_owner,
            }

        return await self.run(read)

    async def list_issues(self, repo, state, page):
        return await self.run(
            lambda client: IssueGateway(client).list_issues(repo, state, page)
        )

    async def detail(self, repo, number):
        return await self.run(lambda client: IssueGateway(client).detail(repo, number))

    def text(self, value: Any, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise ValueError(f"{field}须为 1–{maximum} 个字符")
        value = value.strip()
        token = self.service.settings.value.github_token
        if token:
            value = value.replace(token, "[凭据已隐藏]")
        return redact(value)

    async def prepare(self, data: dict, username: str):
        if not username:
            raise ValueError("请先登录 AstrBot Dashboard")
        action = data.get("action")
        if action not in {"create", "comment", "state"}:
            raise ValueError("不支持的 Issue 操作")
        cfg = self.service.settings.value
        repo = data.get("repo")
        # 先在本地拒绝越权，不向其他账号仓库发请求。
        if (
            not isinstance(repo, str)
            or not cfg.allows_issue(repo)
            or cfg.excludes(repo)
        ):
            raise ValueError("该仓库不在本人 Issue 允许列表中，请先在设置中授权")
        preview = {"action": action, "repo": repo}
        if action == "create":
            preview["title"] = self.text(data.get("title"), "标题", 256)
            preview["body"] = self.text(data.get("body"), "正文", 60000)
        else:
            preview["number"] = IssueGateway.number(data.get("number"))
            if action == "comment":
                preview["body"] = self.text(data.get("body"), "评论", 60000)
            else:
                if data.get("state") not in {"open", "closed"}:
                    raise ValueError("只支持关闭或重开 Issue")
                preview["state"] = data["state"]
        configuration = self.signature()

        async def validate(client):
            gateway = IssueGateway(client)
            await gateway.repository(repo, write=True)
            if action != "create":
                issue = await gateway.get_issue(repo, preview["number"])
                preview["title"] = issue["title"]

        await self.run(validate)
        self.ensure_active()
        if configuration != self.signature():
            raise ValueError("配置已变化，请重新生成预览")
        now = time.time()
        self.drafts = {
            key: draft for key, draft in self.drafts.items() if draft.expires_at > now
        }
        if len(self.drafts) >= 100:
            raise ValueError("待确认操作过多，请稍后再试")
        draft_id = secrets.token_urlsafe(24)
        draft = IssueDraft(username, preview, now + 600, configuration)
        self.drafts[draft_id] = draft
        return {
            "draft_id": draft_id,
            "preview": dict(preview),
            "expires_at": draft.expires_at,
        }

    async def confirm(self, draft_id: Any, username: str):
        if not isinstance(draft_id, str):
            raise ValueError("请先生成操作预览")
        draft = self.drafts.get(draft_id)
        if not draft or draft.username != username or draft.expires_at <= time.time():
            raise ValueError("预览不存在、已过期或不属于当前 Dashboard 用户")
        self.drafts.pop(draft_id)
        if draft.configuration != self.signature():
            raise ValueError("GitHub 配置或权限已变化，请重新预览操作")
        preview = draft.preview
        action, repo = preview["action"], preview["repo"]

        def guard():
            self.ensure_active()
            if draft.configuration != self.signature():
                raise ValueError("GitHub 配置或权限已变化，请重新预览操作")

        async def write(client):
            gateway = IssueGateway(client, write_guard=guard)
            if action == "create":
                result = await gateway.create(repo, preview["title"], preview["body"])
                key, message = "issue", "问题已创建"
            elif action == "comment":
                result = await gateway.add_comment(
                    repo, preview["number"], preview["body"]
                )
                key, message = "comment", "评论已发布"
            else:
                result = await gateway.change_state(
                    repo, preview["number"], preview["state"]
                )
                key, message = (
                    "issue",
                    "问题已关闭" if preview["state"] == "closed" else "问题已重开",
                )
            return {
                "ok": True,
                "action": action,
                "repo": repo,
                key: result,
                "html_url": result["html_url"],
                "message": message,
            }

        try:
            return await self.run(write)
        except Exception as exc:
            # 一旦确认即消费预览；平台超时也可能已受理，禁止自动重放写请求。
            raise ValueError(
                f"未能确认操作结果：{self.service.safe_error(exc)}。请先到 GitHub 核对是否已生效，再重新预览；不要直接重复提交。"
            ) from None
