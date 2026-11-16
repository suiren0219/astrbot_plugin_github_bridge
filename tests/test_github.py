import copy
import time
import unittest
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from github_bridge.github import API, GitHubClient, GitHubError
from github_bridge.settings import Settings

DATE = "2026-11-16T08:00:00Z"
LATER = "2026-11-16T08:01:00Z"
REPO = {
    "id": 1,
    "full_name": "test-owner/demo",
    "owner": {"login": "test-owner"},
    "default_branch": "main",
    "description": "demo",
    "stargazers_count": 1,
    "forks_count": 0,
    "open_issues_count": 0,
    "updated_at": DATE,
}


def commit(sha, parents=None):
    return {
        "sha": sha * 40,
        "parents": [{"sha": p * 40} for p in (parents or [])],
        "commit": {"message": "修复 " + sha, "author": {"date": DATE}},
    }


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = GitHubClient(
            Settings(github_owner="test-owner", github_token="test-secret")
        )
        self.repo = copy.deepcopy(REPO)
        self.head = commit("a")
        self.issues = []
        self.releases = []
        self.comments = []
        self.activities = []
        self.issue_details = {}
        self.compare = None
        self.calls = []
        self.client._request = AsyncMock(side_effect=self.request)

    async def asyncTearDown(self):
        await self.client.close()

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urlsplit(url).path
        if path == "/repositories/1":
            return copy.deepcopy(self.repo), {}
        if path.endswith("/commits"):
            return [copy.deepcopy(self.head)], {}
        if "/compare/" in path:
            return copy.deepcopy(self.compare), {}
        if path.endswith("/releases"):
            return copy.deepcopy(self.releases), {}
        if path.endswith("/issues/events"):
            return copy.deepcopy(self.activities), {}
        if path.endswith("/issues/comments"):
            return copy.deepcopy(self.comments), {}
        if path.endswith("/issues"):
            return copy.deepcopy(self.issues), {}
        if "/issues/" in path:
            return copy.deepcopy(self.issue_details[int(path.rsplit("/", 1)[1])]), {}
        raise AssertionError(path)

    async def test_first_observation_is_silent_even_with_historical_data(self):
        self.releases = [{"id": 10, "published_at": DATE, "draft": False, "name": "v1"}]
        self.issues = [{"id": 20, "number": 1, "created_at": DATE}]
        self.comments = [{"id": 30, "created_at": DATE}]
        self.activities = [
            {"id": 40, "event": "closed", "issue": {"number": 1}, "created_at": DATE}
        ]
        result = await self.client.observe(self.repo, None)
        self.assertEqual(result.events, [])
        self.assertEqual(result.snapshot["sources"]["commits"]["head"], "a" * 40)
        self.assertEqual(
            result.snapshot["sources"]["releases"]["published"], ["10:" + DATE]
        )

    async def test_commit_compare_and_legacy_key_are_stable(self):
        previous = (await self.client.observe(self.repo, None)).snapshot
        self.head = commit("c", ["b"])
        self.compare = {
            "status": "ahead",
            "base_commit": {"sha": "a" * 40},
            "total_commits": 2,
            "commits": [commit("b", ["a"]), self.head],
        }
        result = await self.client.observe(self.repo, previous)
        events = [event for event in result.events if event.kind == "commit"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].legacy_keys, ("test-owner/demo@" + "b" * 40,))
        repeated = await self.client.observe(self.repo, result.snapshot)
        self.assertEqual(repeated.events, [])
        self.assertEqual(previous["sources"]["commits"]["head"], "a" * 40)

    async def test_default_branch_change_baselines_commits_but_reports_metadata(self):
        previous = (await self.client.observe(self.repo, None)).snapshot
        self.repo["default_branch"] = "develop"
        self.head = commit("b")
        result = await self.client.observe(self.repo, previous)
        self.assertEqual([event.kind for event in result.events], ["metadata"])
        self.assertEqual(result.snapshot["sources"]["commits"]["head"], "b" * 40)

    async def test_release_prerelease_and_draft_publish(self):
        self.releases = [{"id": 10, "draft": True, "published_at": None}]
        previous = (await self.client.observe(self.repo, None)).snapshot
        self.releases = [
            {
                "id": 10,
                "draft": False,
                "published_at": DATE,
                "prerelease": True,
                "name": "v2-beta",
            }
        ]
        result = await self.client.observe(self.repo, previous)
        self.assertEqual([event.kind for event in result.events], ["release"])
        self.assertIn("预发布", result.events[0].title)
        self.assertEqual(
            (await self.client.observe(self.repo, result.snapshot)).events, []
        )

    async def test_new_issue_comments_closed_reopened_exclude_pull_requests(self):
        self.issues = [{"id": 20, "number": 1, "created_at": DATE}]
        previous = (await self.client.observe(self.repo, None)).snapshot
        self.issues += [
            {"id": 21, "number": 2, "created_at": LATER, "title": "bug"},
            {"id": 22, "number": 3, "created_at": LATER, "pull_request": {}},
        ]
        self.activities = [
            {"id": i, "event": action, "issue": issue, "created_at": LATER}
            for i, action, issue in [
                (30, "closed", {"number": 2}),
                (31, "reopened", {"number": 2}),
                (32, "closed", {"number": 3, "pull_request": {}}),
            ]
        ]
        self.comments = [
            {
                "id": 40,
                "created_at": LATER,
                "issue_url": API + "/repos/test-owner/demo/issues/2",
                "body": "详情",
            },
            {
                "id": 41,
                "created_at": LATER,
                "issue_url": API + "/repos/test-owner/demo/issues/3",
                "body": "PR",
            },
        ]
        self.issue_details = {
            2: {"number": 2, "title": "bug"},
            3: {"number": 3, "pull_request": {}},
        }
        # 固定空数据源的时间，模拟基线后出现的评论。
        previous["sources"]["comments"]["since"] = "2026-11-16T07:59:00Z"
        result = await self.client.observe(self.repo, previous)
        self.assertCountEqual(
            [event.kind for event in result.events],
            ["issue", "issue-event", "issue-event", "comment"],
        )
        self.assertFalse(any("/issues/3" in event.url for event in result.events))
        self.assertEqual(
            (await self.client.observe(self.repo, result.snapshot)).events, []
        )

    async def test_deleted_comment_anchor_does_not_block_future_comments(self):
        self.comments = [{"id": 40, "created_at": DATE}]
        previous = (await self.client.observe(self.repo, None)).snapshot
        self.comments = [
            {
                "id": 41,
                "created_at": LATER,
                "issue_url": API + "/repos/test-owner/demo/issues/1",
                "body": "new",
            }
        ]
        self.issue_details = {1: {"number": 1}}
        result = await self.client.observe(self.repo, previous)
        self.assertEqual([event.kind for event in result.events], ["comment"])

    async def test_reenabled_source_is_silent_and_metadata_cycle_not_deduped(self):
        previous = (await self.client.observe(self.repo, None)).snapshot
        self.client.settings.monitor_releases = False
        disabled = (await self.client.observe(self.repo, previous)).snapshot
        self.releases = [{"id": 10, "published_at": DATE, "draft": False}]
        self.client.settings.monitor_releases = True
        self.assertEqual((await self.client.observe(self.repo, disabled)).events, [])
        keys = []
        for stars in (2, 1, 2):
            self.repo["stargazers_count"] = stars
            observation = await self.client.observe(self.repo, previous)
            keys.extend(
                event.key for event in observation.events if event.kind == "metadata"
            )
            previous = observation.snapshot
        self.assertEqual(len(set(keys)), 3)

    async def test_failed_source_leaves_previous_snapshot_intact(self):
        previous = (await self.client.observe(self.repo, None)).snapshot
        original = copy.deepcopy(previous)
        self.client._collect_comments = AsyncMock(side_effect=GitHubError("failure"))
        with self.assertRaises(GitHubError):
            await self.client.observe(self.repo, previous)
        self.assertEqual(previous, original)

    async def test_disabled_issues_skip_issue_endpoints(self):
        self.repo["has_issues"] = False
        result = await self.client.observe(self.repo, None)
        self.assertFalse(result.snapshot["sources"]["issues"]["enabled"])
        self.assertFalse(any("/issues" in call[1] for call in self.calls))

    async def test_issue_label_changes_are_reported(self):
        previous = (await self.client.observe(self.repo, None)).snapshot
        self.activities = [
            {
                "id": 90,
                "event": "labeled",
                "issue": {"number": 1, "title": "bug"},
                "created_at": LATER,
            }
        ]
        result = await self.client.observe(self.repo, previous)
        self.assertEqual(len(result.events), 1)
        self.assertIn("添加标签", result.events[0].title)

    async def test_force_push_errors_instead_of_silently_losing_history(self):
        previous = (await self.client.observe(self.repo, None)).snapshot
        self.head = commit("b")
        self.compare = {"status": "diverged"}
        with self.assertRaisesRegex(GitHubError, "历史被改写"):
            await self.client.observe(self.repo, previous)


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_pages_are_followed_and_limit_is_explicit(self):
        client = GitHubClient(Settings(max_pages=2))
        first = API + "/user/repos?per_page=100"
        second = first + "&page=2"
        client._request = AsyncMock(
            side_effect=[
                ([{"id": 1}], {"link": f'<{second}>; rel="next"'}),
                ([{"id": 2}], {}),
            ]
        )
        self.assertEqual(await client._list("/user/repos"), [{"id": 1}, {"id": 2}])
        client.settings.max_pages = 1
        client._request = AsyncMock(
            return_value=([{"id": 1}], {"link": f'<{second}>; rel="next"'})
        )
        with self.assertRaisesRegex(GitHubError, "max_pages"):
            await client._list("/user/repos")

    async def test_foreign_links_loops_and_skipped_pages_are_rejected(self):
        client = GitHubClient(Settings())
        current = API + "/user/repos?per_page=100"
        for target in (
            "https://evil.example/repos?page=2",
            current + "&page=1",
            current + "&page=3",
        ):
            with self.subTest(target=target), self.assertRaises(GitHubError):
                client._next(f'<{target}>; rel="next"', current)

    async def test_owned_repository_discovery_filters_collaborators_and_reports_login(
        self,
    ):
        client = GitHubClient(Settings(github_owner="test-owner", github_token="test"))
        client._request = AsyncMock(
            side_effect=[
                ({"login": "test-owner"}, {}),
                ({"login": "test-owner", "type": "User"}, {}),
                ([REPO, {**REPO, "id": 2, "owner": {"login": "thirdparty"}}], {}),
            ]
        )
        info = await client.test_connection()
        self.assertEqual(info["login"], "test-owner")
        self.assertEqual(info["repository_count"], 1)
        self.assertIn("affiliation=owner", client._request.call_args_list[-1].args[1])

    async def test_wrong_token_owner_is_rejected_before_repo_discovery(self):
        client = GitHubClient(Settings(github_owner="test-owner", github_token="test"))
        client._request = AsyncMock(
            side_effect=[
                ({"login": "other"}, {}),
                ({"login": "test-owner", "type": "User"}, {}),
            ]
        )
        with self.assertRaisesRegex(GitHubError, "不一致"):
            await client.list_repositories()
        self.assertEqual(client._request.await_count, 2)


class Response:
    def __init__(self, status, headers=None, data=None):
        self.status, self.headers, self.data = status, headers or {}, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, **kwargs):
        return self.data


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_retry_and_token_not_leaked(self):
        client = GitHubClient(
            Settings(github_owner="test-owner", github_token="private-test-secret")
        )
        future = int(time.time()) + 180

        class Session:
            def request(self, *args, **kwargs):
                self.kwargs = kwargs
                return Response(
                    403,
                    {
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Limit": "5000",
                        "X-RateLimit-Reset": str(future),
                    },
                    {"message": "private-test-secret"},
                )

        client._session = Session()
        with self.assertRaises(GitHubError) as error:
            await client._request("GET", "/user")
        self.assertGreaterEqual(error.exception.retry_at, future)
        self.assertNotIn("private-test-secret", str(error.exception))
        self.assertFalse(client._session.kwargs["allow_redirects"])
        self.assertEqual(client.rate_limit["remaining"], 0)
        with self.assertRaisesRegex(GitHubError, "退避"):
            await client._request("GET", "/user")

    async def test_external_origin_and_redirects_never_receive_followup(self):
        client = GitHubClient(Settings(github_owner="test-owner", github_token="test"))
        with self.assertRaises(GitHubError):
            await client._request("GET", "https://evil.example")
        self.assertIsNone(client._session)

        class Session:
            def request(self, *args, **kwargs):
                return Response(302, {"Location": "https://evil.example"})

        client._session = Session()
        with self.assertRaisesRegex(GitHubError, "重定向"):
            await client._request("GET", "/user")
