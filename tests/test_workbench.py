import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from github_bridge.github import GitHubClient, GitHubError
from github_bridge.service import BridgeService
from github_bridge.settings import SettingsStore
from github_bridge.workbench import IssueWorkbench

REPO = "test-owner/demo"
REPO_PATH = f"/repos/{REPO}"
USERNAME = "dashboard-admin"
NOW = 1800000000.0


def repository(name=REPO, **changes):
    return {
        "id": 1,
        "full_name": name,
        "owner": {"login": "test-owner"},
        "description": "测试仓库",
        "private": True,
        "archived": False,
        "has_issues": True,
        "open_issues_count": 1,
        "html_url": f"https://github.com/{name}",
        **changes,
    }


def issue(number=7, **changes):
    return {
        "number": number,
        "title": "原问题",
        "body": "问题正文",
        "state": "open",
        "user": {"login": "reporter"},
        "labels": [],
        "comments": 1,
        "html_url": "https://untrusted.example/issues/7",
        **changes,
    }


def comment(**changes):
    return {
        "id": 101,
        "body": "评论正文",
        "user": {"login": "helper"},
        "html_url": "javascript:alert(1)",
        **changes,
    }


class FakeClientFactory:
    """使用真实客户端校验逻辑，但所有网络调用都必须预先安排。"""

    def __init__(self):
        self.clients = []
        self.request = AsyncMock(side_effect=AssertionError("测试未安排的 GitHub 请求"))

    def __call__(self, settings):
        client = GitHubClient(settings)
        client._request = self.request
        client.close = AsyncMock(wraps=client.close)
        self.clients.append(client)
        return client


class IssueWorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {"GITHUB_BRIDGE_TOKEN": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SettingsStore(
            Path(self.temp.name),
            {
                "github_owner": "test-owner",
                "github_token": "unit-test-secret",
                "issue_repositories": "demo, project-*",
                "excluded_repositories": "blocked",
            },
        )
        self.factory = FakeClientFactory()
        self.service = SimpleNamespace(
            settings=self.settings,
            client_factory=self.factory,
            cooldown_until=0,
            _rate_error=Mock(),
        )
        # 绑定真实脱敏器，既不启动 BridgeService 后台任务，也不接触 GitHub。
        self.service.safe_error = Mock(
            side_effect=lambda error: BridgeService.safe_error(self.service, error)
        )
        self.workbench = IssueWorkbench(self.service)
        self.clock = patch("github_bridge.workbench.time.time", return_value=NOW)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)

    async def asyncTearDown(self):
        for client in self.factory.clients:
            self.assertIsNone(client._session)
            client.close.assert_awaited_once_with()

    def data(self, action="create", **changes):
        result = {"action": action, "repo": REPO}
        if action == "create":
            result.update(title="  新问题  ", body="  正文  ")
        elif action == "comment":
            result.update(number=7, body="  新评论  ")
        else:
            result.update(number=7, state="closed")
        result.update(changes)
        return result

    async def prepare(self, action="create", **changes):
        self.factory.request.side_effect = [(repository(), {})]
        if action != "create":
            self.factory.request.side_effect = [(repository(), {}), (issue(), {})]
        return await self.workbench.prepare(self.data(action, **changes), USERNAME)

    def assert_no_writes(self):
        self.assertTrue(
            all(
                entry.args[0] == "GET" for entry in self.factory.request.await_args_list
            )
        )

    async def test_repositories_keep_readonly_repos_and_describe_write_capability(self):
        repos = [
            repository(),
            repository("test-owner/read-only", id=2),
            repository("test-owner/project-old", id=3, archived=True),
            repository("test-owner/project-disabled", id=4, has_issues=False),
            repository("test-owner/blocked", id=5),
            repository("someone/demo", id=6, owner={"login": "someone"}),
        ]
        self.factory.request.side_effect = [
            ({"login": "test-owner"}, {}),
            ({"login": "test-owner", "type": "User"}, {}),
            (repos, {}),
        ]
        result = await self.workbench.repositories()
        self.assertEqual(result["owner"], "test-owner")
        self.assertEqual(
            {item["full_name"]: item["can_write"] for item in result["items"]},
            {
                REPO: True,
                "test-owner/read-only": False,
                "test-owner/project-old": False,
                "test-owner/project-disabled": False,
            },
        )
        self.assertNotIn("owner", result["items"][0])
        self.assert_no_writes()

    async def test_readonly_issue_list_and_detail_do_not_require_allowlist(self):
        self.settings.update({"issue_repositories": ""})
        self.factory.request.side_effect = [
            (repository(), {}),
            ([issue(), issue(8, pull_request={})], {}),
            (repository(), {}),
            (issue(), {}),
            ([comment()], {}),
        ]
        listing = await self.workbench.list_issues(REPO, "open", 1)
        detail = await self.workbench.detail(REPO, 7)
        self.assertEqual([item["number"] for item in listing["items"]], [7])
        self.assertFalse(listing["can_write"])
        self.assertFalse(detail["can_write"])
        self.assertEqual(detail["comments"][0]["id"], 101)
        self.assertEqual(
            detail["issue"]["html_url"], f"https://github.com/{REPO}/issues/7"
        )
        self.assert_no_writes()

    async def test_prepare_all_actions_only_reads_and_returns_normalized_preview(self):
        for action in ("create", "comment", "state"):
            with self.subTest(action=action):
                self.factory.request.reset_mock()
                prepared = await self.prepare(action)
                preview = prepared["preview"]
                self.assertEqual((preview["action"], preview["repo"]), (action, REPO))
                self.assertEqual(prepared["expires_at"], NOW + 600)
                self.assertIn(prepared["draft_id"], self.workbench.drafts)
                self.assert_no_writes()
                if action == "create":
                    self.assertEqual(
                        (preview["title"], preview["body"]), ("新问题", "正文")
                    )
                    self.factory.request.assert_awaited_once_with("GET", REPO_PATH)
                else:
                    self.assertEqual(preview["number"], 7)
                    self.assertEqual(preview["title"], "原问题")
                    self.assertEqual(self.factory.request.await_count, 2)
                    if action == "comment":
                        self.assertEqual(preview["body"], "新评论")
                    else:
                        self.assertEqual(preview["state"], "closed")

    async def test_prepare_rejects_foreign_excluded_and_unlisted_repos_without_network(
        self,
    ):
        for repo in (
            "other/demo",
            "test-owner/blocked",
            "test-owner/unlisted",
            "test-owner/..",
            None,
        ):
            with (
                self.subTest(repo=repo),
                self.assertRaisesRegex(ValueError, "允许列表"),
            ):
                await self.workbench.prepare(self.data(repo=repo), USERNAME)
        self.assertEqual(self.workbench.drafts, {})
        self.assertEqual(self.factory.clients, [])
        self.factory.request.assert_not_awaited()

    async def test_prepare_validates_login_action_text_number_and_state_locally(self):
        cases = [
            (self.data(), ""),
            (self.data(action="delete"), USERNAME),
            (self.data(title=" "), USERNAME),
            (self.data(title="x" * 257), USERNAME),
            (self.data(body="x" * 60001), USERNAME),
            (self.data(body=None), USERNAME),
            (self.data("comment", number=True), USERNAME),
            (self.data("comment", number=0), USERNAME),
            (self.data("comment", body=" "), USERNAME),
            (self.data("state", state="merged"), USERNAME),
        ]
        for data, username in cases:
            with (
                self.subTest(action=data["action"], username=username),
                self.assertRaises(ValueError),
            ):
                await self.workbench.prepare(data, username)
        self.assertEqual(self.factory.clients, [])
        self.assertEqual(self.workbench.drafts, {})
        self.factory.request.assert_not_awaited()

    async def test_prepare_rejects_pr_comments_and_state_without_write(self):
        for action in ("comment", "state"):
            with self.subTest(action=action):
                self.factory.request.reset_mock()
                self.factory.request.side_effect = [
                    (repository(), {}),
                    (issue(pull_request={}), {}),
                ]
                with self.assertRaisesRegex(ValueError, "Pull Request"):
                    await self.workbench.prepare(self.data(action), USERNAME)
                self.assertEqual(self.workbench.drafts, {})
                self.assertEqual(self.factory.request.await_count, 2)
                self.assert_no_writes()

    async def test_confirm_creates_once_and_consumes_preview(self):
        prepared = await self.prepare()
        self.factory.request.reset_mock()
        self.factory.request.side_effect = [
            (repository(), {}),
            (issue(12, title="新问题"), {}),
        ]
        result = await self.workbench.confirm(prepared["draft_id"], USERNAME)
        self.assertTrue(result["ok"])
        self.assertEqual(
            (result["action"], result["message"]), ("create", "问题已创建")
        )
        self.assertEqual(result["html_url"], f"https://github.com/{REPO}/issues/12")
        self.assertEqual(result["issue"]["number"], 12)
        self.assertNotIn(prepared["draft_id"], self.workbench.drafts)
        self.assertEqual(
            self.factory.request.await_args_list,
            [
                call("GET", REPO_PATH),
                call(
                    "POST",
                    REPO_PATH + "/issues",
                    json_body={"title": "新问题", "body": "正文"},
                ),
            ],
        )
        with self.assertRaisesRegex(ValueError, "预览不存在"):
            await self.workbench.confirm(prepared["draft_id"], USERNAME)
        self.assertEqual(self.factory.request.await_count, 2)
        self.assertEqual(len(self.factory.clients), 2)

    async def test_confirm_comments_once_after_reading_current_issue(self):
        prepared = await self.prepare("comment")
        self.factory.request.reset_mock()
        self.factory.request.side_effect = [
            (repository(), {}),
            (issue(), {}),
            (comment(body="新评论"), {}),
        ]
        result = await self.workbench.confirm(prepared["draft_id"], USERNAME)
        self.assertEqual(
            (result["action"], result["message"]), ("comment", "评论已发布")
        )
        self.assertEqual(result["comment"]["body"], "新评论")
        self.assertEqual(
            result["html_url"], f"https://github.com/{REPO}/issues/7#issuecomment-101"
        )
        self.assertEqual(
            self.factory.request.await_args_list,
            [
                call("GET", REPO_PATH),
                call("GET", REPO_PATH + "/issues/7"),
                call(
                    "POST",
                    REPO_PATH + "/issues/7/comments",
                    json_body={"body": "新评论"},
                ),
            ],
        )
        with self.assertRaises(ValueError):
            await self.workbench.confirm(prepared["draft_id"], USERNAME)
        self.assertEqual(self.factory.request.await_count, 3)

    async def test_confirm_closes_and_reopens_with_one_patch_each(self):
        for state, message in (("closed", "问题已关闭"), ("open", "问题已重开")):
            with self.subTest(state=state):
                prepared = await self.prepare("state", state=state)
                self.factory.request.reset_mock()
                self.factory.request.side_effect = [
                    (repository(), {}),
                    (issue(), {}),
                    (issue(state=state), {}),
                ]
                result = await self.workbench.confirm(prepared["draft_id"], USERNAME)
                self.assertEqual(result["message"], message)
                self.assertEqual(result["issue"]["state"], state)
                self.assertEqual(
                    self.factory.request.await_args_list,
                    [
                        call("GET", REPO_PATH),
                        call("GET", REPO_PATH + "/issues/7"),
                        call(
                            "PATCH", REPO_PATH + "/issues/7", json_body={"state": state}
                        ),
                    ],
                )

    async def test_preview_is_bound_to_dashboard_username_without_stealing_it(self):
        prepared = await self.prepare()
        self.factory.request.reset_mock()
        with self.assertRaisesRegex(ValueError, "当前 Dashboard 用户"):
            await self.workbench.confirm(prepared["draft_id"], "other-admin")
        self.factory.request.assert_not_awaited()
        self.assertIn(prepared["draft_id"], self.workbench.drafts)
        self.factory.request.side_effect = [(repository(), {}), (issue(12), {})]
        self.assertTrue(
            (await self.workbench.confirm(prepared["draft_id"], USERNAME))["ok"]
        )

    async def test_expiry_boundary_rejects_preview_before_any_request(self):
        prepared = await self.prepare()
        self.factory.request.reset_mock()
        self.clock_mock.return_value = prepared["expires_at"]
        with self.assertRaisesRegex(ValueError, "过期"):
            await self.workbench.confirm(prepared["draft_id"], USERNAME)
        self.factory.request.assert_not_awaited()
        self.assertEqual(len(self.factory.clients), 1)

    async def test_unknown_and_nonstring_draft_ids_never_contact_github(self):
        for identifier in (None, 7, {}, "missing"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                await self.workbench.confirm(identifier, USERNAME)
        self.factory.request.assert_not_awaited()
        self.assertEqual(self.factory.clients, [])

    async def test_token_owner_allowlist_and_exclusion_changes_invalidate_preview(self):
        original = {
            "github_token": "unit-test-secret",
            "github_owner": "test-owner",
            "issue_repositories": "demo, project-*",
            "excluded_repositories": "blocked",
        }
        for change in (
            {"github_token": "replacement-secret"},
            {"github_owner": "another-owner"},
            {"issue_repositories": "project-*"},
            {"excluded_repositories": "demo"},
        ):
            with self.subTest(change=change):
                self.settings.update(original)
                prepared = await self.prepare()
                self.factory.request.reset_mock()
                self.settings.update(change)
                with self.assertRaisesRegex(ValueError, "配置或权限已变化"):
                    await self.workbench.confirm(prepared["draft_id"], USERNAME)
                self.assertNotIn(prepared["draft_id"], self.workbench.drafts)
                with self.assertRaisesRegex(ValueError, "预览不存在"):
                    await self.workbench.confirm(prepared["draft_id"], USERNAME)
                self.factory.request.assert_not_awaited()

    async def test_permission_changed_while_confirmation_is_reading_blocks_write(self):
        prepared = await self.prepare()
        self.factory.request.reset_mock()

        async def change_during_read(method, path, **kwargs):
            self.assertEqual(method, "GET")
            self.settings.update({"issue_repositories": ""})
            return repository(), {}

        self.factory.request.side_effect = change_during_read
        with self.assertRaisesRegex(ValueError, "配置或权限已变化"):
            await self.workbench.confirm(prepared["draft_id"], USERNAME)
        self.assert_no_writes()
        self.assertNotIn(prepared["draft_id"], self.workbench.drafts)

    async def test_plugin_stopped_during_confirmation_prevents_write(self):
        prepared = await self.prepare()
        self.factory.request.reset_mock()

        async def stop_during_read(method, path, **kwargs):
            self.assertEqual(method, "GET")
            self.service._closed = True
            return repository(), {}

        self.factory.request.side_effect = stop_during_read
        with self.assertRaisesRegex(ValueError, "插件正在关闭"):
            await self.workbench.confirm(prepared["draft_id"], USERNAME)
        self.assert_no_writes()
        self.assertNotIn(prepared["draft_id"], self.workbench.drafts)

    async def test_unrelated_configuration_change_does_not_invalidate_preview(self):
        prepared = await self.prepare()
        self.settings.update({"poll_interval": 600})
        self.factory.request.side_effect = [(repository(), {}), (issue(12), {})]
        self.assertTrue(
            (await self.workbench.confirm(prepared["draft_id"], USERNAME))["ok"]
        )

    async def test_configuration_change_during_prepare_cannot_create_draft(self):
        async def change_config(method, path, **kwargs):
            self.settings.update({"github_token": "replacement-secret"})
            return repository(), {}

        self.factory.request.side_effect = change_config
        with self.assertRaisesRegex(ValueError, "配置已变化"):
            await self.workbench.prepare(self.data(), USERNAME)
        self.assertEqual(self.workbench.drafts, {})
        self.factory.request.assert_awaited_once_with("GET", REPO_PATH)

    async def test_confirm_revalidates_pr_and_archived_repository_before_writing(self):
        for failure in ("pr", "archived"):
            with self.subTest(failure=failure):
                prepared = await self.prepare("comment")
                self.factory.request.reset_mock()
                if failure == "pr":
                    self.factory.request.side_effect = [
                        (repository(), {}),
                        (issue(pull_request={}), {}),
                    ]
                else:
                    self.factory.request.side_effect = [(repository(archived=True), {})]
                with self.assertRaisesRegex(ValueError, "未能确认操作结果"):
                    await self.workbench.confirm(prepared["draft_id"], USERNAME)
                self.assert_no_writes()
                self.assertNotIn(prepared["draft_id"], self.workbench.drafts)

    async def test_failed_confirmation_is_consumed_and_never_replays_write(self):
        for action in ("create", "comment", "state"):
            with self.subTest(action=action):
                prepared = await self.prepare(action)
                self.factory.request.reset_mock()
                responses = [(repository(), {})]
                if action != "create":
                    responses.append((issue(), {}))
                failure = TimeoutError(
                    "network timeout unit-test-secret Bearer failure-secret"
                )
                self.factory.request.side_effect = [*responses, failure]
                with self.assertRaisesRegex(ValueError, "先到 GitHub 核对") as error:
                    await self.workbench.confirm(prepared["draft_id"], USERNAME)
                self.assertNotIn("unit-test-secret", str(error.exception))
                self.assertNotIn("failure-secret", str(error.exception))
                self.assertNotIn(prepared["draft_id"], self.workbench.drafts)
                calls_after_failure = self.factory.request.await_count
                with self.assertRaisesRegex(ValueError, "预览不存在"):
                    await self.workbench.confirm(prepared["draft_id"], USERNAME)
                self.assertEqual(self.factory.request.await_count, calls_after_failure)
                methods = [
                    entry.args[0] for entry in self.factory.request.await_args_list
                ]
                self.assertEqual(
                    methods,
                    ["GET"] * len(responses)
                    + ["PATCH" if action == "state" else "POST"],
                )
                self.service._rate_error.assert_called_with(failure)

    async def test_concurrent_confirmation_cannot_duplicate_post(self):
        prepared = await self.prepare()
        self.factory.request.reset_mock()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pending_request(method, path, **kwargs):
            if method == "GET":
                entered.set()
                await release.wait()
                return repository(), {}
            return issue(12), {}

        self.factory.request.side_effect = pending_request
        first = asyncio.create_task(
            self.workbench.confirm(prepared["draft_id"], USERNAME)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            # 第一份确认尚在等待 GitHub；第二份必须已无法取得同一预览。
            with self.assertRaisesRegex(ValueError, "预览不存在"):
                await self.workbench.confirm(prepared["draft_id"], USERNAME)
        finally:
            release.set()
            results = await asyncio.gather(first, return_exceptions=True)
        self.assertIsInstance(results[0], dict)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(
            [entry.args[0] for entry in self.factory.request.await_args_list],
            ["GET", "POST"],
        )

    async def test_preview_and_outgoing_write_redact_all_supported_secret_forms(self):
        secrets = [
            "unit-test-secret",
            "ghp_fakePrivate123",
            "github_pat_fakePrivate456",
            "password-value",
            "api-value",
            "bearer-value",
        ]
        text = "普通文本 unit-test-secret ghp_fakePrivate123 github_pat_fakePrivate456 password=password-value api_key=api-value Bearer bearer-value"
        prepared = await self.prepare(title=text, body=text)
        preview_json = json.dumps(prepared, ensure_ascii=False)
        self.assertIn("普通文本", preview_json)
        self.assertIn("隐藏", preview_json)
        for secret in secrets:
            self.assertNotIn(secret, preview_json)
        self.factory.request.reset_mock()
        self.factory.request.side_effect = [(repository(), {}), (issue(12), {})]
        await self.workbench.confirm(prepared["draft_id"], USERNAME)
        outgoing = self.factory.request.await_args_list[-1].kwargs["json_body"]
        self.assertEqual(
            outgoing,
            {
                "title": prepared["preview"]["title"],
                "body": prepared["preview"]["body"],
            },
        )
        for secret in secrets:
            self.assertNotIn(secret, json.dumps(outgoing))

    async def test_read_failure_is_sanitized_reported_and_client_closed(self):
        failure = GitHubError(
            "failure unit-test-secret ghp_fakePrivate123 Bearer bearer-value",
            retry_at=NOW + 60,
        )
        self.factory.request.side_effect = failure
        with self.assertRaises(ValueError) as error:
            await self.workbench.list_issues(REPO, "open", 1)
        self.assertIn("failure", str(error.exception))
        for secret in ("unit-test-secret", "ghp_fakePrivate123", "bearer-value"):
            self.assertNotIn(secret, str(error.exception))
        self.service._rate_error.assert_called_once_with(failure)
        self.service.safe_error.assert_called_once_with(failure)
        self.assertEqual(self.workbench.drafts, {})

    async def test_missing_credentials_and_cooldown_prevent_client_creation(self):
        for changes, cooldown, message in (
            ({"clear_token": True}, 0, "Token"),
            ({"github_owner": ""}, 0, "用户名"),
            ({}, NOW + 1, "退避"),
        ):
            with self.subTest(changes=changes, cooldown=cooldown):
                self.settings.update(
                    {"github_owner": "test-owner", "github_token": "unit-test-secret"}
                )
                self.settings.update(changes)
                self.service.cooldown_until = cooldown
                with self.assertRaisesRegex(ValueError, message):
                    await self.workbench.repositories()
        self.assertEqual(self.factory.clients, [])
        self.factory.request.assert_not_awaited()

    async def test_prepare_prunes_expired_drafts_and_limits_live_drafts(self):
        prepared = await self.prepare()
        self.clock_mock.return_value = prepared["expires_at"]
        fresh = await self.prepare()
        self.assertNotIn(prepared["draft_id"], self.workbench.drafts)
        self.assertIn(fresh["draft_id"], self.workbench.drafts)
        live = self.workbench.drafts[fresh["draft_id"]]
        self.workbench.drafts = {f"draft-{index}": live for index in range(100)}
        self.factory.request.side_effect = [(repository(), {})]
        with self.assertRaisesRegex(ValueError, "待确认操作过多"):
            await self.workbench.prepare(self.data(), USERNAME)
        self.assertEqual(len(self.workbench.drafts), 100)
        self.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
