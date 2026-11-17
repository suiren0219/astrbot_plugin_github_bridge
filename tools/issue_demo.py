"""中文工作台的内存 GitHub API；所有写入仅用于本地演示。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from github_bridge.github import GitHubClient


def now():
    return datetime.now(timezone.utc).isoformat()


class DemoIssueAPI:
    _name = staticmethod(GitHubClient._name)
    _url = staticmethod(GitHubClient._url)
    _next = GitHubClient._next
    _safe = GitHubClient._safe
    issue_data: dict = {}
    comment_data: dict = {}

    @classmethod
    def reset_issues(cls):
        stamp = now()
        cls.issue_data = {
            "demo-app": [
                {
                    "number": 1,
                    "title": "导入失败后缺少错误提示",
                    "body": "## 复现步骤\n1. 打开导入页面\n2. 选择空文件\n\n预期显示中文错误提示，实际页面没有反馈。",
                    "state": "open",
                    "user": {"login": "demo-user"},
                    "labels": [{"name": "缺陷", "color": "d73a4a"}],
                    "comments": 1,
                    "created_at": stamp,
                    "updated_at": stamp,
                },
                {
                    "number": 2,
                    "title": "支持记住列表筛选条件",
                    "body": "希望重新打开工作台时保留上次选择的状态。",
                    "state": "open",
                    "user": {"login": "demo-user"},
                    "labels": [{"name": "建议", "color": "a2eeef"}],
                    "comments": 0,
                    "created_at": stamp,
                    "updated_at": stamp,
                },
                {
                    "number": 3,
                    "title": "更新帮助文档中的操作说明",
                    "body": "示例问题，已完成。",
                    "state": "closed",
                    "user": {"login": "demo-user"},
                    "labels": [],
                    "comments": 0,
                    "created_at": stamp,
                    "updated_at": stamp,
                },
            ]
        }
        cls.comment_data = {
            ("demo-app", 1): [
                {
                    "id": 1,
                    "body": "已确认，可以在这里补充复现信息。",
                    "user": {"login": "demo-user"},
                    "created_at": stamp,
                }
            ]
        }

    async def _request(self, method, path, *, params=None, json_body=None):
        parts = urlsplit(path)
        segments = parts.path.strip("/").split("/")
        if len(segments) < 3 or segments[0] != "repos":
            raise ValueError("演示 API 不支持这个操作")
        full_name = "/".join(segments[1:3])
        repos = self.repos()
        repo = next((item for item in repos if item["full_name"] == full_name), None)
        if repo is None:
            raise ValueError("演示仓库不存在")
        if len(segments) == 3:
            return copy.deepcopy(repo), {}
        name = repo["full_name"].split("/", 1)[1]
        if segments[3] != "issues":
            raise ValueError("演示 API 仅支持 Issue")
        items = self.issue_data.setdefault(name, [])
        if len(segments) == 4:
            if method == "GET":
                query = parse_qs(parts.query)
                state = query.get("state", ["open"])[0]
                page = int(query.get("page", ["1"])[0])
                selected = [
                    item
                    for item in reversed(items)
                    if state == "all" or item["state"] == state
                ]
                return copy.deepcopy(selected[(page - 1) * 30 : page * 30]), {}
            if method == "POST":
                number = max((item["number"] for item in items), default=0) + 1
                item = {
                    "number": number,
                    "title": json_body["title"],
                    "body": json_body["body"],
                    "state": "open",
                    "user": {"login": "demo-user"},
                    "labels": [],
                    "comments": 0,
                    "created_at": now(),
                    "updated_at": now(),
                }
                items.append(item)
                return copy.deepcopy(item), {}
        number = int(segments[4])
        item = next((value for value in items if value["number"] == number), None)
        if item is None:
            raise ValueError("演示问题不存在")
        if len(segments) == 5:
            if method == "PATCH":
                item["state"] = json_body["state"]
                item["updated_at"] = now()
            return copy.deepcopy(item), {}
        if segments[5] == "comments":
            comments = self.comment_data.setdefault((name, number), [])
            if method == "POST":
                comment = {
                    "id": sum(len(items) for items in self.comment_data.values()) + 1,
                    "body": json_body["body"],
                    "user": {"login": "demo-user"},
                    "created_at": now(),
                }
                comments.append(comment)
                item["comments"] = len(comments)
                item["updated_at"] = now()
                return copy.deepcopy(comment), {}
            return copy.deepcopy(comments), {}
        raise ValueError("演示 API 不支持这个操作")

    async def create_issue(self, full_name, title, body):
        raw, _ = await self._request(
            "POST",
            f"/repos/{full_name}/issues",
            json_body={"title": title, "body": body},
        )
        return raw
