import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from astrbot.api import logger as astrbot_logger

from github_bridge.models import Event, Observation
from github_bridge.service import BridgeService
from github_bridge.settings import SettingsStore
from github_bridge.storage import Store
from tools.preview_server import PreviewServer


class FakeGitHub:
    def __init__(self, settings):
        self.settings = settings
        self.rate_limit = {"remaining": 4900, "limit": 5000}
        self.fail = False
        self.closed = False
        self.repo = {"id": 1, "full_name": f"{settings.github_owner}/demo"}

    async def close(self):
        self.closed = True

    async def list_repositories(self):
        if self.fail:
            raise RuntimeError("GitHub temporarily offline")
        return [self.repo]

    async def observe(self, repo, previous):
        events = (
            []
            if previous is None
            else [
                Event(
                    "new",
                    repo["full_name"],
                    "commit",
                    "fix",
                    "body",
                    "https://github.com/a/b",
                    "2026-11-16T00:00:00Z",
                )
            ]
        )
        return Observation({"seen": True}, events)

    async def test_connection(self):
        return {"login": "test-owner", "repository_count": 1}

    async def create_issue(self, repo, title, body):
        return {"html_url": f"https://github.com/{repo}/issues/1", "number": 1}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_preview_cli_starts_without_astrbot_host(self):
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "tools/preview.py", "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--port PORT", result.stdout)

    def test_service_uses_astrbot_api_logger(self):
        import github_bridge.service as service_module

        self.assertIs(service_module.logger, astrbot_logger)
        source = inspect.getsource(service_module)
        self.assertNotIn("import logging", source)
        self.assertNotIn("logging.getLogger", source)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = SettingsStore(
            self.root,
            {
                "github_owner": "test-owner",
                "issue_repositories": "demo-app",
                "github_token": "unit-test-secret",
                "target_umo": "platform:FriendMessage:1",
                "enabled": True,
            },
        )
        self.store = Store(self.root / "bridge.sqlite3")
        self.sent = []

        async def sender(target, content):
            self.sent.append((target, content))

        self.service = BridgeService(self.settings, self.store, sender, FakeGitHub)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_first_run_silent_then_send_only_once(self):
        await self.service.check()
        self.assertEqual(self.sent, [])
        await self.service.check()
        await self.service.check()
        self.assertEqual(len(self.sent), 1)
        self.assertIn(":FriendMessage:", self.sent[0][0])
        self.assertEqual(self.store.counts("test-owner")["sent"], 1)

    async def test_queued_single_line_commit_has_one_subject_and_is_delivered_once(
        self,
    ):
        cfg = self.settings.value
        repo = self.service.client.repo
        title = "fix: 修复提交通知重复标题"
        url = "https://github.com/test-owner/demo/commit/" + "a" * 40
        event = Event(
            "commit:" + "a" * 40, repo["full_name"], "commit", title, title, url, ""
        )
        observation = Observation({"seen": True}, [event])
        self.store.discover(cfg.identity, [repo])
        self.assertEqual(
            self.store.record(cfg.identity, cfg.target_umo, repo, observation), 1
        )
        self.assertEqual(
            self.store.record(cfg.identity, cfg.target_umo, repo, observation), 0
        )
        # 重试已排队消息时 GitHub 可以离线，不需要重新采集或修改原始正文。
        self.service.client.fail = True

        await self.service.check()
        await self.service.check()

        self.assertEqual(
            self.sent,
            [
                (
                    cfg.target_umo,
                    f"[GitHub · 代码提交]\n{repo['full_name']}\n{title}\n{url}",
                )
            ],
        )
        self.assertEqual(self.store.counts(cfg.identity)["sent"], 1)
        self.assertEqual(self.store.events(cfg.identity, "sent")[0]["body"], title)

    async def test_failed_send_not_counted_as_delivered_and_retry_works_offline(self):
        async def failing_sender(target, content):
            raise RuntimeError("QQ offline unit-test-secret")

        self.service.sender = failing_sender
        await self.service.check()
        await self.service.check()
        self.assertEqual(self.store.counts("test-owner")["sent"], 0)
        failed = self.store.events("test-owner", "failed")
        self.assertEqual(len(failed), 1)
        self.assertNotIn("unit-test-secret", failed[0]["last_error"])
        self.store.retry("test-owner", self.settings.value.target_umo)
        self.service.client.fail = True

        async def working_sender(target, content):
            self.sent.append(content)

        self.service.sender = working_sender
        await self.service.check()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.counts("test-owner")["sent"], 1)

    async def test_pause_does_not_send(self):
        await self.service.update_settings({"enabled": False})
        with self.assertRaises(ValueError):
            self.service.request_check()
        self.assertEqual(await self.service.check(), {"queued": 0, "sent": 0})
        self.assertEqual(self.sent, [])

    async def test_per_cycle_cap_keeps_remaining_queue(self):
        cfg = self.settings.value
        self.store.discover(cfg.identity, [self.service.client.repo])
        events = [
            Event(
                f"e{i}",
                "test-owner/demo",
                "commit",
                "title",
                "body",
                "https://github.com/a/b",
                "",
            )
            for i in range(8)
        ]
        self.store.record(
            cfg.identity,
            cfg.target_umo,
            self.service.client.repo,
            Observation({"seen": True}, events),
        )
        await self.service.check()
        self.assertEqual(len(self.sent), 5)
        self.assertGreater(self.store.counts(cfg.identity)["pending"], 0)

    async def test_cooldown_stops_github_calls_but_drains_existing_queue(self):
        cfg = self.settings.value
        repo = self.service.client.repo
        self.store.discover(cfg.identity, [repo])
        self.store.record(
            cfg.identity,
            cfg.target_umo,
            repo,
            Observation(
                {},
                [
                    Event(
                        "cooldown", repo["full_name"], "commit", "title", "body", "", ""
                    )
                ],
            ),
        )
        self.service.cooldown_until = time.time() + 3600
        self.service.client.list_repositories = AsyncMock()
        result = await self.service.check()
        self.service.client.list_repositories.assert_not_awaited()
        self.assertEqual(result["sent"], 1)

    async def test_issue_write_is_allowlisted(self):
        with self.assertRaises(ValueError):
            await self.service.create_issue("someone/demo-app", "title", "body")
        issue = await self.service.create_issue("demo-app", "title", "body")
        self.assertEqual(issue["number"], 1)


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = SettingsStore(
            root,
            {
                "github_owner": "test-owner",
                "github_token": "never-return-this",
                "target_umo": "platform:FriendMessage:1",
            },
        )
        self.store = Store(root / "state.sqlite3")

        async def no_send(target, text):
            raise AssertionError("Panel test must not send real messages")

        self.service = BridgeService(self.settings, self.store, no_send, FakeGitHub)
        self.panel = PreviewServer(self.service, root)
        self.client = TestClient(TestServer(self.panel.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()
        self.temp.cleanup()

    async def test_dashboard_status_and_token_write_only(self):
        response = await self.client.get("/api/status")
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ready"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = await self.client.get("/api/settings")
        data = await response.json()
        self.assertTrue(data["token_configured"])
        self.assertNotIn("never-return-this", json.dumps(data))
        self.assertNotIn("github_token", data)

    async def test_private_target_validation_and_settings_update(self):
        response = await self.client.post(
            "/api/settings", json={"target_umo": "QQ:GroupMessage:1"}
        )
        self.assertEqual(response.status, 400)
        self.assertIn(":FriendMessage:", self.settings.value.target_umo)
        response = await self.client.post("/api/settings", json={"enabled": True})
        self.assertEqual(response.status, 200)
        self.assertTrue(self.settings.value.enabled)

    async def test_no_plugin_login_or_logout_routes(self):
        for endpoint in ("login", "logout", "session"):
            response = await self.client.post(f"/api/{endpoint}", json={})
            self.assertEqual(response.status, 404)

    async def test_legacy_import_endpoint_is_idempotent(self):
        sent = {"test-owner/demo-app/main": {"a" * 40: [[]]}}
        response = await self.client.post(
            "/api/import", json={"sent": sent, "failed": []}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["imported"], 1)
        response = await self.client.post(
            "/api/import", json={"sent": sent, "failed": []}
        )
        self.assertEqual((await response.json())["imported"], 0)
        response = await self.client.post(
            "/api/import", json={"sent": [], "failed": []}
        )
        self.assertEqual(response.status, 400)

    async def test_unknown_route_is_safe_and_preview_has_security_headers(self):
        response = await self.client.get("/api/missing")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await response.json())["error"], "未找到该接口")
