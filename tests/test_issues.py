import unittest
from unittest.mock import AsyncMock, call
from urllib.parse import parse_qs, urlsplit

from github_bridge.github import API, GitHubClient, GitHubError
from github_bridge.issues import IssueGateway
from github_bridge.settings import Settings

REPO = "test-owner/demo"
REPO_PATH = f"/repos/{REPO}"


def repository(**changes):
    return {"full_name": REPO, "has_issues": True, "archived": False, **changes}


def issue(number=7, **changes):
    return {
        "number": number,
        "title": "需要修复的问题",
        "body": "复现步骤",
        "state": "open",
        "user": {"login": "reporter"},
        "labels": [{"name": "bug", "color": "ff0000"}],
        "comments": 1,
        "created_at": "2026-11-17T00:00:00Z",
        "updated_at": "2026-11-17T01:00:00Z",
        "html_url": "https://untrusted.example/issue",
        **changes,
    }


def comment(identifier=101, **changes):
    return {
        "id": identifier,
        "body": "已经复现",
        "user": {"login": "helper"},
        "created_at": "2026-11-17T02:00:00Z",
        "html_url": "javascript:alert(1)",
        **changes,
    }


class IssueGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(
            github_owner="test-owner",
            github_token="unit-test-secret",
            issue_repositories="demo, test-owner/project-*",
            excluded_repositories="blocked, test-owner/project-private",
        )
        self.client = GitHubClient(self.settings)
        self.client._request = AsyncMock(
            side_effect=AssertionError("测试未安排的 GitHub 请求")
        )
        self.gateway = IssueGateway(self.client)
        self.addAsyncCleanup(self.client.close)

    def assert_read_only_requests(self):
        self.assertTrue(
            all(
                request.args[0] == "GET"
                for request in self.client._request.await_args_list
            )
        )
        self.assertIsNone(self.client._session)

    async def test_repo_scope_excludes_and_allowlist_are_checked_before_requests(self):
        for name in (REPO, "TEST-OWNER/DEMO", "test-owner/project-one"):
            with self.subTest(allowed=name):
                self.assertEqual(self.gateway.repo_name(name, write=True), name)
        self.assertEqual(
            self.gateway.repo_name("test-owner/read-only"), "test-owner/read-only"
        )
        for name in (
            "other/demo",
            "test-owner/blocked",
            "TEST-OWNER/PROJECT-PRIVATE",
            "test-owner/read-only",
            "https://github.com/test-owner/demo",
            "test-owner/../demo",
            "test-owner/..",
            None,
        ):
            with (
                self.subTest(rejected=name),
                self.assertRaises((ValueError, GitHubError)),
            ):
                await self.gateway.repository(name, write=True)
        self.client._request.assert_not_awaited()

    async def test_repository_identity_and_issue_availability_are_verified(self):
        for raw, error in (
            ([], GitHubError),
            (repository(full_name="other/demo"), GitHubError),
            (repository(has_issues=False), ValueError),
        ):
            with self.subTest(raw=raw):
                self.client._request.reset_mock()
                self.client._request.side_effect = [(raw, {})]
                with self.assertRaises(error):
                    await self.gateway.repository(REPO)
                self.client._request.assert_awaited_once_with("GET", REPO_PATH)
        self.client._request.side_effect = [
            (repository(full_name="TEST-OWNER/DEMO"), {})
        ]
        self.assertEqual(
            (await self.gateway.repository(REPO))["full_name"], "TEST-OWNER/DEMO"
        )

    async def test_readonly_and_archived_repositories_can_list_without_write_permission(
        self,
    ):
        for allowed, archived in (("", False), ("demo", True)):
            with self.subTest(allowed=allowed, archived=archived):
                self.settings.issue_repositories = allowed
                self.client._request.reset_mock()
                self.client._request.side_effect = [
                    (repository(archived=archived), {}),
                    ([issue()], {}),
                ]
                result = await self.gateway.list_issues(REPO, "open", 1)
                self.assertEqual([item["number"] for item in result["items"]], [7])
                self.assertFalse(result["can_write"])
                self.assertEqual(self.client._request.await_count, 2)
                self.assert_read_only_requests()

    async def test_archived_repository_rejects_writes_after_metadata_read(self):
        self.client._request.side_effect = [(repository(archived=True), {})]
        with self.assertRaisesRegex(ValueError, "归档"):
            await self.gateway.create(REPO, "标题", "正文")
        self.client._request.assert_awaited_once_with("GET", REPO_PATH)

    async def test_list_pagination_filters_prs_without_changing_page_semantics(self):
        first = (
            API
            + REPO_PATH
            + "/issues?state=all&sort=updated&direction=desc&per_page=30&page=2"
        )
        next_page = first.replace("page=2", "page=3")
        self.client._request.side_effect = [
            (repository(), {}),
            (
                [issue(7), issue(8, pull_request={}), issue(9, state="closed")],
                {"link": f'<{next_page}>; rel="next"'},
            ),
            (repository(), {}),
            ([issue(10, pull_request={})], {}),
        ]
        result = await self.gateway.list_issues(REPO, "all", 2)
        self.assertEqual([item["number"] for item in result["items"]], [7, 9])
        self.assertEqual(
            (result["page"], result["has_next"], result["repo"], result["can_write"]),
            (2, True, REPO, True),
        )
        request_url = self.client._request.await_args_list[1].args[1]
        self.assertEqual(urlsplit(request_url).netloc, "api.github.com")
        self.assertEqual(
            parse_qs(urlsplit(request_url).query),
            {
                "state": ["all"],
                "sort": ["updated"],
                "direction": ["desc"],
                "per_page": ["30"],
                "page": ["2"],
            },
        )
        last = await self.gateway.list_issues(REPO, "all", 3)
        self.assertEqual(last["items"], [])
        self.assertFalse(last["has_next"])
        self.assertEqual(last["page"], 3)
        self.assertEqual(self.client._request.await_count, 4)
        self.assert_read_only_requests()

    async def test_list_rejects_invalid_queries_and_malformed_responses(self):
        for state, page in (
            ("merged", 1),
            ("open", True),
            ("open", 0),
            ("open", 10001),
            ("open", "1"),
        ):
            with self.subTest(state=state, page=page):
                self.client._request.reset_mock()
                self.client._request.side_effect = [(repository(), {})]
                with self.assertRaises(ValueError):
                    await self.gateway.list_issues(REPO, state, page)
                self.client._request.assert_awaited_once_with("GET", REPO_PATH)
        for raw in ({"items": []}, [None], [issue(state="merged")]):
            with self.subTest(raw=raw):
                self.client._request.side_effect = [(repository(), {}), (raw, {})]
                with self.assertRaises(GitHubError):
                    await self.gateway.list_issues(REPO, "open", 1)

    async def test_foreign_pagination_url_is_rejected_without_following_it(self):
        self.client._request.side_effect = [
            (repository(), {}),
            ([issue()], {"link": '<https://evil.example/steal?page=2>; rel="next"'}),
        ]
        with self.assertRaises(GitHubError):
            await self.gateway.list_issues(REPO, "open", 1)
        self.assertEqual(self.client._request.await_count, 2)
        self.assert_read_only_requests()

    async def test_detail_loads_comments_pages_and_rebuilds_all_browser_urls(self):
        first = API + REPO_PATH + "/issues/7/comments?per_page=100"
        second = first + "&page=2"
        self.client._request.side_effect = [
            (repository(), {}),
            (issue(), {}),
            ([comment(101)], {"link": f'<{second}>; rel="next"'}),
            ([comment(102, user=None)], {}),
        ]
        result = await self.gateway.detail(REPO, 7)
        self.assertEqual(
            result["issue"]["html_url"], f"https://github.com/{REPO}/issues/7"
        )
        self.assertEqual(
            result["issue"]["labels"], [{"name": "bug", "color": "ff0000"}]
        )
        self.assertEqual([item["id"] for item in result["comments"]], [101, 102])
        self.assertEqual(
            result["comments"][0]["html_url"],
            f"https://github.com/{REPO}/issues/7#issuecomment-101",
        )
        self.assertEqual(result["comments"][1]["author"], "")
        self.assertFalse(result["comments_truncated"])
        self.assertTrue(result["can_write"])
        self.client._request.assert_has_awaits(
            [
                call("GET", REPO_PATH),
                call("GET", REPO_PATH + "/issues/7"),
                call("GET", first),
                call("GET", second),
            ]
        )
        self.assert_read_only_requests()

    async def test_detail_truncates_at_configured_limit_or_five_hundred_comments(self):
        first = API + REPO_PATH + "/issues/7/comments?per_page=100"
        for configured, limit in ((1, 1), (3, 3), (20, 5)):
            with self.subTest(max_pages=configured):
                self.settings.max_pages = configured
                self.client._request.reset_mock()
                pages = []
                for page in range(1, limit + 1):
                    comments = [
                        comment((page - 1) * 100 + index) for index in range(1, 101)
                    ]
                    pages.append(
                        (comments, {"link": f'<{first}&page={page + 1}>; rel="next"'})
                    )
                self.client._request.side_effect = [
                    (repository(), {}),
                    (issue(), {}),
                    *pages,
                ]
                result = await self.gateway.detail(REPO, 7)
                self.assertEqual(len(result["comments"]), limit * 100)
                self.assertTrue(result["comments_truncated"])
                self.assertEqual(self.client._request.await_count, 2 + limit)
                self.assert_read_only_requests()

    async def test_detail_rejects_malformed_comments(self):
        for raw in (
            {"comments": []},
            [None],
            [comment(identifier=True)],
            [comment(identifier=0)],
        ):
            with self.subTest(raw=raw):
                self.client._request.side_effect = [
                    (repository(), {}),
                    (issue(), {}),
                    (raw, {}),
                ]
                with self.assertRaises(GitHubError):
                    await self.gateway.detail(REPO, 7)

    async def test_pr_is_rejected_for_detail_comment_and_both_state_changes(self):
        for action in ("detail", "comment", "closed", "open"):
            with self.subTest(action=action):
                self.client._request.reset_mock()
                self.client._request.side_effect = [
                    (repository(), {}),
                    (issue(pull_request={}), {}),
                ]
                with self.assertRaisesRegex(ValueError, "Pull Request"):
                    if action == "detail":
                        await self.gateway.detail(REPO, 7)
                    elif action == "comment":
                        await self.gateway.add_comment(REPO, 7, "不应发布")
                    else:
                        await self.gateway.change_state(REPO, 7, action)
                self.assertEqual(self.client._request.await_count, 2)
                self.assert_read_only_requests()

    async def test_number_validation_and_issue_identity_prevent_writes(self):
        for number in (True, False, 0, -1, 2147483648, "7", 7.0, None):
            with self.subTest(number=number), self.assertRaises(ValueError):
                await self.gateway.get_issue(REPO, number)
        self.client._request.assert_not_awaited()
        self.client._request.side_effect = [(issue(number=8), {})]
        with self.assertRaisesRegex(GitHubError, "编号不匹配"):
            await self.gateway.get_issue(REPO, 7)
        self.assert_read_only_requests()

    async def test_create_sends_exactly_one_post_and_rebuilds_result_url(self):
        self.client._request.side_effect = [(repository(), {}), (issue(12), {})]
        result = await self.gateway.create(REPO, "  新问题  ", "正文")
        self.assertEqual(result["number"], 12)
        self.assertEqual(result["html_url"], f"https://github.com/{REPO}/issues/12")
        self.assertEqual(
            self.client._request.await_args_list,
            [
                call("GET", REPO_PATH),
                call(
                    "POST",
                    REPO_PATH + "/issues",
                    json_body={"title": "新问题", "body": "正文"},
                ),
            ],
        )

    async def test_comment_sends_exactly_one_post_after_verifying_issue(self):
        self.client._request.side_effect = [
            (repository(), {}),
            (issue(), {}),
            (comment(), {}),
        ]
        result = await self.gateway.add_comment(REPO, 7, "确认复现")
        self.assertEqual(
            result["html_url"], f"https://github.com/{REPO}/issues/7#issuecomment-101"
        )
        self.assertEqual(
            self.client._request.await_args_list,
            [
                call("GET", REPO_PATH),
                call("GET", REPO_PATH + "/issues/7"),
                call(
                    "POST",
                    REPO_PATH + "/issues/7/comments",
                    json_body={"body": "确认复现"},
                ),
            ],
        )

    async def test_close_and_reopen_send_one_patch_with_requested_state(self):
        for state in ("closed", "open"):
            with self.subTest(state=state):
                self.client._request.reset_mock()
                self.client._request.side_effect = [
                    (repository(), {}),
                    (issue(), {}),
                    (issue(state=state), {}),
                ]
                result = await self.gateway.change_state(REPO, 7, state)
                self.assertEqual(result["state"], state)
                self.assertEqual(
                    self.client._request.await_args_list,
                    [
                        call("GET", REPO_PATH),
                        call("GET", REPO_PATH + "/issues/7"),
                        call(
                            "PATCH", REPO_PATH + "/issues/7", json_body={"state": state}
                        ),
                    ],
                )

    async def test_invalid_state_and_mismatched_patch_response_are_rejected(self):
        self.client._request.side_effect = [(repository(), {}), (issue(), {})]
        with self.assertRaises(ValueError):
            await self.gateway.change_state(REPO, 7, "merged")
        self.assert_read_only_requests()
        self.client._request.reset_mock()
        self.client._request.side_effect = [
            (repository(), {}),
            (issue(), {}),
            (issue(8, state="closed"), {}),
        ]
        with self.assertRaisesRegex(GitHubError, "编号不匹配"):
            await self.gateway.change_state(REPO, 7, "closed")
        self.assertEqual(
            [entry.args[0] for entry in self.client._request.await_args_list],
            ["GET", "GET", "PATCH"],
        )

    async def test_failed_post_is_never_retried(self):
        for action in ("create", "comment"):
            with self.subTest(action=action):
                self.client._request.reset_mock()
                reads = [(repository(), {})]
                if action == "comment":
                    reads.append((issue(), {}))
                self.client._request.side_effect = [*reads, TimeoutError("结果未知")]
                with self.assertRaises(TimeoutError):
                    if action == "create":
                        await self.gateway.create(REPO, "标题", "正文")
                    else:
                        await self.gateway.add_comment(REPO, 7, "评论")
                methods = [
                    entry.args[0] for entry in self.client._request.await_args_list
                ]
                self.assertEqual(methods, ["GET"] * len(reads) + ["POST"])

    async def test_issue_and_comment_text_redact_token_bearer_and_control_characters(
        self,
    ):
        secret = self.settings.github_token
        text = f"普通文本 {secret} Bearer other-secret\x00"
        result = self.gateway.issue(
            issue(
                title=text,
                body=text,
                user={"login": text},
                labels=[{"name": text, "color": "123456789"}],
            ),
            REPO,
        )
        reply = self.gateway.comment(comment(body=text, user={"login": text}), REPO, 7)
        for value in (
            result["title"],
            result["body"],
            result["author"],
            result["labels"][0]["name"],
            reply["body"],
            reply["author"],
        ):
            with self.subTest(value=value):
                self.assertIn("普通文本", value)
                self.assertIn("[REDACTED]", value)
                self.assertNotIn(secret, value)
                self.assertNotIn("other-secret", value)
                self.assertNotIn("\x00", value)
        self.assertEqual(result["labels"][0]["color"], "123456")


if __name__ == "__main__":
    unittest.main()
