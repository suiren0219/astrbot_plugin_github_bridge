import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from github_bridge.service import BridgeService
from github_bridge.settings import SettingsStore
from github_bridge.storage import Store
from tools.preview import DemoGitHub
from tools.preview_server import PreviewServer


class WorkbenchAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = SettingsStore(
            self.root,
            {
                "github_owner": "demo-user",
                "github_token": "fake-preview-only",
                "issue_repositories": "demo-app,demo-calendar",
            },
        )

        class MemoryGitHub(DemoGitHub):
            async def list_repositories(self):
                return self.repos()

        MemoryGitHub.reset_issues()
        self.github = MemoryGitHub
        self.store = Store(self.root / "state.sqlite3")
        self.sender = AsyncMock()
        self.service = BridgeService(
            self.settings, self.store, self.sender, MemoryGitHub
        )
        self.panel = PreviewServer(self.service, self.root)
        self.client = TestClient(TestServer(self.panel.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()
        self.sender.assert_not_awaited()
        self.temp.cleanup()

    async def prepare(self, action, **payload):
        response = await self.client.post(
            "/api/issue_prepare",
            json={"repo": "demo-user/demo-app", "action": action, **payload},
        )
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def confirm(self, draft):
        response = await self.client.post(
            "/api/issue_confirm", json={"draft_id": draft["draft_id"]}
        )
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def test_create_comment_close_reopen_only_after_confirmation(self):
        before = len(self.github.issue_data["demo-app"])
        draft = await self.prepare(
            "create", title="中文工作台测试", body="复现步骤\n1. 导入示例文件"
        )
        self.assertEqual(len(self.github.issue_data["demo-app"]), before)
        created = await self.confirm(draft)
        number = created["issue"]["number"]
        self.assertEqual(len(self.github.issue_data["demo-app"]), before + 1)
        self.assertEqual(created["issue"]["title"], "中文工作台测试")
        replay = await self.client.post(
            "/api/issue_confirm", json={"draft_id": draft["draft_id"]}
        )
        self.assertEqual(replay.status, 400)
        self.assertEqual(len(self.github.issue_data["demo-app"]), before + 1)

        draft = await self.prepare("comment", number=number, body="已经补充复现信息")
        self.assertEqual(self.github.comment_data.get(("demo-app", number), []), [])
        await self.confirm(draft)
        response = await self.client.get(
            "/api/issue_detail", params={"repo": "demo-user/demo-app", "number": number}
        )
        detail = await response.json()
        self.assertEqual(detail["comments"][0]["body"], "已经补充复现信息")
        self.assertEqual(detail["issue"]["comments"], 1)

        for state in ("closed", "open"):
            draft = await self.prepare("state", number=number, state=state)
            result = await self.confirm(draft)
            self.assertEqual(result["issue"]["state"], state)
        self.assertTrue(detail["can_write"])

    async def test_readonly_and_archived_repositories_cannot_prepare_writes(self):
        response = await self.client.get("/api/issue_repositories")
        self.assertEqual(response.status, 200)
        repos = {repo["full_name"]: repo for repo in (await response.json())["items"]}
        self.assertTrue(repos["demo-user/demo-app"]["can_write"])
        self.assertFalse(repos["demo-user/demo-chat"]["can_write"])
        self.assertFalse(repos["demo-user/demo-web"]["can_write"])
        for repo in (
            "demo-user/demo-chat",
            "demo-user/demo-web",
            "other-owner/demo-app",
        ):
            response = await self.client.post(
                "/api/issue_prepare",
                json={
                    "repo": repo,
                    "action": "create",
                    "title": "标题",
                    "body": "正文",
                },
            )
            self.assertEqual(response.status, 400)
        response = await self.client.get(
            "/api/issues",
            params={"repo": "demo-user/demo-chat", "state": "all", "page": 1},
        )
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["can_write"])

    async def test_invalid_page_and_issue_numbers_are_rejected(self):
        for endpoint, params in (
            ("issues", {"repo": "demo-user/demo-app", "page": "abc"}),
            ("issues", {"repo": "demo-user/demo-app", "page": "0"}),
            ("issue_detail", {"repo": "demo-user/demo-app", "number": "1/../../other"}),
            ("issue_detail", {"repo": "demo-user/demo-app", "number": "-1"}),
        ):
            response = await self.client.get("/api/" + endpoint, params=params)
            self.assertEqual(response.status, 400)

    async def test_preview_is_invalid_after_permission_change(self):
        draft = await self.prepare("create", title="待确认的问题", body="正文")
        self.settings.update({"issue_repositories": ""})
        response = await self.client.post(
            "/api/issue_confirm", json={"draft_id": draft["draft_id"]}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.github.issue_data["demo-app"]), 3)
