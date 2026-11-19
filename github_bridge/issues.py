"""GitHub Issue 工作台的受限 REST 适配层。"""

from __future__ import annotations

from typing import Any

from .github import GitHubError


class IssueGateway:
    def __init__(self, client, write_guard=None):
        self.client = client
        self.settings = client.settings
        self.write_guard = write_guard

    def check_write(self):
        if self.write_guard is not None:
            self.write_guard()

    def repo_name(self, value: Any, *, write: bool = False) -> str:
        name = self.client._name(value)
        if name.split("/", 1)[0].casefold() != self.settings.identity:
            raise ValueError("工作台只允许操作配置账号名下的仓库")
        if self.settings.excludes(name):
            raise ValueError("该仓库已被排除，请先在设置中调整")
        if write and not self.settings.allows_issue(name):
            raise ValueError("该仓库不在 Issue 允许列表中，请先在设置中明确授权")
        return name

    async def repository(self, name: str, *, write: bool = False) -> dict:
        name = self.repo_name(name, write=write)
        repo, _ = await self.client._request("GET", f"/repos/{name}")
        if (
            not isinstance(repo, dict)
            or str(repo.get("full_name", "")).casefold() != name.casefold()
        ):
            raise GitHubError("GitHub 返回的仓库身份不匹配")
        if repo.get("has_issues") is False:
            raise ValueError("该仓库没有启用 Issues，请先在 GitHub 仓库设置中开启")
        if write and repo.get("archived"):
            raise ValueError("已归档仓库只允许查看")
        return repo

    def can_write(self, repo: dict) -> bool:
        return bool(
            self.settings.allows_issue(repo["full_name"])
            and not repo.get("archived")
            and repo.get("has_issues") is not False
        )

    @staticmethod
    def number(value: Any) -> int:
        if type(value) is not int or value < 1 or value > 2147483647:
            raise ValueError("Issue 编号必须是正整数")
        return value

    def issue(self, raw: dict, repo: str) -> dict:
        if not isinstance(raw, dict) or "pull_request" in raw:
            raise ValueError("该编号是 Pull Request，当前工作台仅处理 Issue")
        number = self.number(raw.get("number"))
        if raw.get("state") not in {"open", "closed"}:
            raise GitHubError("GitHub 返回的 Issue 状态无效")
        user = raw.get("user") or {}
        labels = []
        for label in raw.get("labels") or []:
            if isinstance(label, dict):
                labels.append(
                    {
                        "name": self.client._safe(label.get("name"), 100),
                        "color": str(label.get("color", ""))[:6],
                    }
                )
        return {
            "number": number,
            "title": self.client._safe(raw.get("title"), 256),
            "body": self.client._safe(raw.get("body"), 60000),
            "state": raw["state"],
            "author": self.client._safe(
                user.get("login") if isinstance(user, dict) else "", 100
            ),
            "labels": labels,
            "comments": raw.get("comments", 0),
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
            "html_url": f"https://github.com/{repo}/issues/{number}",
        }

    def comment(self, raw: dict, repo: str, number: int) -> dict:
        if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] < 1:
            raise GitHubError("GitHub 返回的评论标识无效")
        user = raw.get("user") or {}
        return {
            "id": raw["id"],
            "body": self.client._safe(raw.get("body"), 60000),
            "author": self.client._safe(
                user.get("login") if isinstance(user, dict) else "", 100
            ),
            "created_at": raw.get("created_at"),
            "html_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{raw['id']}",
        }

    async def list_issues(self, repo: str, state: str, page: int) -> dict:
        repo = self.repo_name(repo)
        metadata = await self.repository(repo)
        if state not in {"open", "closed", "all"}:
            raise ValueError("状态必须为 open、closed 或 all")
        if type(page) is not int or not 1 <= page <= 10000:
            raise ValueError("页码必须为 1–10000 的整数")
        from urllib.parse import urlencode

        url = (
            self.client._url(f"/repos/{repo}/issues")
            + "?"
            + urlencode(
                {
                    "state": state,
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 30,
                    "page": page,
                }
            )
        )
        raw, headers = await self.client._request("GET", url)
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise GitHubError("GitHub 返回的问题列表无效")
        return {
            "items": [
                self.issue(item, repo) for item in raw if "pull_request" not in item
            ],
            "page": page,
            "has_next": self.client._next(headers.get("link", ""), url) is not None,
            "repo": repo,
            "can_write": self.can_write(metadata),
        }

    async def get_issue(self, repo: str, number: int) -> dict:
        repo = self.repo_name(repo)
        number = self.number(number)
        raw, _ = await self.client._request("GET", f"/repos/{repo}/issues/{number}")
        issue = self.issue(raw, repo)
        if issue["number"] != number:
            raise GitHubError("GitHub 返回的 Issue 编号不匹配")
        return issue

    async def detail(self, repo: str, number: int) -> dict:
        repo = self.repo_name(repo)
        metadata = await self.repository(repo)
        issue = await self.get_issue(repo, number)
        url = (
            self.client._url(f"/repos/{repo}/issues/{number}/comments")
            + "?per_page=100"
        )
        comments = []
        # 详情只预览前 500 条评论，超过时明确提示并提供 GitHub 链接。
        for _ in range(min(self.settings.max_pages, 5)):
            raw, headers = await self.client._request("GET", url)
            if not isinstance(raw, list) or any(
                not isinstance(item, dict) for item in raw
            ):
                raise GitHubError("GitHub 返回的评论列表无效")
            comments.extend(self.comment(item, repo, number) for item in raw)
            url = self.client._next(headers.get("link", ""), url)
            if url is None:
                break
        return {
            "issue": issue,
            "comments": comments,
            "comments_truncated": url is not None,
            "can_write": self.can_write(metadata),
            "repo": repo,
        }

    async def create(self, repo: str, title: str, body: str) -> dict:
        await self.repository(repo, write=True)
        self.check_write()
        raw = await self.client.create_issue(repo, title, body)
        return self.issue(raw, repo)

    async def add_comment(self, repo: str, number: int, body: str) -> dict:
        await self.repository(repo, write=True)
        await self.get_issue(repo, number)  # 不允许借评论接口写入 PR。
        self.check_write()
        raw, _ = await self.client._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json_body={"body": body}
        )
        return self.comment(raw, repo, number)

    async def change_state(self, repo: str, number: int, state: str) -> dict:
        await self.repository(repo, write=True)
        await self.get_issue(repo, number)
        if state not in {"open", "closed"}:
            raise ValueError("只支持关闭或重开 Issue")
        self.check_write()
        raw, _ = await self.client._request(
            "PATCH", f"/repos/{repo}/issues/{number}", json_body={"state": state}
        )
        issue = self.issue(raw, repo)
        if issue["number"] != number:
            raise GitHubError("GitHub 返回的 Issue 编号不匹配")
        return issue
