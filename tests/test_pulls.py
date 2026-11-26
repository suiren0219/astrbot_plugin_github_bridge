import copy
import json
import unittest
from dataclasses import asdict
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

from github_bridge.github import API, GitHubClient, GitHubError
from github_bridge.pulls import PullRequests
from github_bridge.settings import Settings

DATE = "2026-11-16T08:00:00Z"
LATER = "2026-11-16T08:01:00Z"
OLDER = "2026-11-16T07:58:59Z"
REPO = {"id": 1, "full_name": "test-owner/demo", "private": False}
PATH = "/repos/test-owner/demo/pulls"


def pull(number=1, **changes):
    return {
        "number": number,
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40, "repo": copy.deepcopy(REPO)},
        "state": "open",
        "merged_at": None,
        "draft": False,
        "title": "修复问题",
        "body": "修改说明",
        "created_at": DATE,
        "updated_at": DATE,
        "html_url": "https://evil.example/ignored",
        **changes,
    }


def file_item(filename="src/main.py", **changes):
    return {
        "filename": filename,
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "patch": "@@ -1 +1 @@\n-old\n+new",
        "raw_url": "https://evil.example/raw",
        "blob_url": "https://evil.example/blob",
        **changes,
    }


async def pull_event(detail):
    client = GitHubClient(Settings(github_owner="test-owner"))
    client._request = AsyncMock(return_value=([], {}))
    state, _ = await PullRequests(client).collect(REPO, None, DATE)
    client._request.return_value = ([detail], {})
    _, events = await PullRequests(client).collect(REPO, state, DATE)
    event = asdict(events[0])
    event["event_key"] = event.pop("key")
    await client.close()
    return event


class PullCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = GitHubClient(
            Settings(github_owner="test-owner", github_token="private-test-secret")
        )
        self.pulls = PullRequests(self.client)
        self.pages = [[pull()]]
        self.calls = []
        self.client._request = AsyncMock(side_effect=self.request)

    async def asyncTearDown(self):
        await self.client.close()

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        parts = urlsplit(url)
        self.assertEqual(parts.netloc, "api.github.com")
        self.assertEqual(parts.path, PATH)
        query = parse_qs(parts.query)
        self.assertEqual(query["state"], ["all"])
        self.assertEqual(query["sort"], ["updated"])
        self.assertEqual(query["direction"], ["desc"])
        page = int(query.get("page", ["1"])[0])
        data = self.pages[page - 1]
        if isinstance(data, Exception):
            raise data
        headers = {}
        if page < len(self.pages):
            next_url = url.split("&page=", 1)[0] + f"&page={page + 1}"
            headers["link"] = f'<{next_url}>; rel="next"'
        return copy.deepcopy(data), headers

    async def baseline(self):
        state, events = await self.pulls.collect(REPO, None, DATE)
        self.assertEqual(events, [])
        return state

    async def test_baseline_only_keeps_recent_window_and_is_silent(self):
        self.pages = [[pull(), pull(2, updated_at=OLDER)], [pull(3, updated_at=OLDER)]]
        state = await self.baseline()
        self.assertEqual(state["since"], "2026-11-16T07:59:00Z")
        self.assertEqual(set(state["seen"]), {"1"})
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("body", state["seen"]["1"])
        self.assertEqual(state["revision"], 1)

    async def test_empty_old_and_disabled_sources_establish_silent_baseline(self):
        for old in (None, {}, {"enabled": False}, {"since": OLDER, "ids": [1]}):
            with self.subTest(old=old):
                _, events = await self.pulls.collect(REPO, old, DATE)
                self.assertEqual(events, [])
        self.pages = [[]]
        state = await self.baseline()
        self.assertEqual(state["seen"], {})

    async def test_incremental_retry_and_unchanged_dedup(self):
        old = await self.baseline()
        original = copy.deepcopy(old)
        self.pages = [[pull(head={"sha": "c" * 40}, updated_at=LATER)]]
        state, events = await self.pulls.collect(REPO, old, LATER)
        repeated_state, repeated_events = await self.pulls.collect(REPO, old, LATER)
        self.assertEqual(state, repeated_state)
        self.assertEqual(events, repeated_events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "pull-request")
        self.assertIn("更新代码", events[0].title)
        self.assertEqual(events[0].url, "https://github.com/test-owner/demo/pull/1")
        self.assertIn("c" * 40, events[0].body)
        self.assertIn("b" * 40, events[0].body)
        self.assertRegex(
            events[0].key, r"^github:1:pull:1:c{40}:b{40}:[0-9a-f]{64}:[0-9a-f]{24}$"
        )
        self.assertEqual((await self.pulls.collect(REPO, state, LATER))[1], [])
        self.assertEqual(old, original)

    async def test_new_pr_base_change_and_updated_at_only_are_reported(self):
        old = await self.baseline()
        for changes in (
            {"base": {"sha": "d" * 40}},
            {"updated_at": LATER},
            {"body": "更新正文"},
            {"draft": True},
        ):
            with self.subTest(changes=changes):
                self.pages = [[pull(**changes)]]
                _, events = await self.pulls.collect(REPO, old, LATER)
                self.assertEqual(len(events), 1)
        self.pages = [[pull(2, created_at=LATER, updated_at=LATER), pull()]]
        _, events = await self.pulls.collect(REPO, old, LATER)
        self.assertEqual(len(events), 1)
        self.assertIn("新建 PR #2", events[0].title)

    async def test_title_cycle_close_reopen_and_merge_have_distinct_keys(self):
        old = await self.baseline()
        keys = []
        for title in ("B", "修复问题", "B"):
            self.pages = [[pull(title=title)]]
            old, events = await self.pulls.collect(REPO, old, DATE)
            self.assertIn("修改信息", events[0].title)
            keys.append(events[0].key)
        self.assertEqual(len(set(keys)), 3)
        for changes, label in (
            ({"state": "closed"}, "关闭"),
            ({"state": "open"}, "重开"),
            ({"state": "closed", "merged_at": LATER}, "合并"),
        ):
            self.pages = [[pull(**changes)]]
            old, events = await self.pulls.collect(REPO, old, DATE)
            self.assertIn(label, events[0].title)

    async def test_same_second_boundary_is_read_across_pages(self):
        old = await self.baseline()
        boundary = old["since"]
        self.pages = [
            [pull(), pull(2, updated_at=boundary)],
            [pull(3, updated_at=boundary)],
            [pull(4, updated_at=OLDER)],
            [pull(5, updated_at=OLDER)],
        ]
        self.calls.clear()
        state, events = await self.pulls.collect(REPO, old, LATER)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([event.url.rsplit("/", 1)[1] for event in events], ["2", "3"])
        self.assertEqual(set(state["seen"]), {"1"})
        self.assertEqual(state["since"], DATE)

    async def test_old_window_entries_are_checked_then_expired(self):
        self.pages = [[pull(created_at=OLDER, updated_at="2026-11-16T07:59:20Z")]]
        old = await self.baseline()
        state, events = await self.pulls.collect(REPO, old, LATER)
        self.assertEqual(events, [])
        self.assertEqual(state["seen"], {})
        self.pages = [[pull(created_at=OLDER, updated_at="2026-11-16T08:02:00Z")]]
        _, events = await self.pulls.collect(REPO, state, "2026-11-16T08:02:00Z")
        self.assertEqual(len(events), 1)
        self.assertIn("活动更新", events[0].title)

    async def test_expired_closed_and_merged_pr_activity_is_not_a_new_transition(self):
        for merged in (None, OLDER):
            with self.subTest(merged=merged):
                self.pages = [
                    [
                        pull(
                            state="closed",
                            merged_at=merged,
                            closed_at=OLDER,
                            created_at=OLDER,
                            updated_at="2026-11-16T07:59:20Z",
                        )
                    ]
                ]
                old = await self.baseline()
                expired, events = await self.pulls.collect(REPO, old, LATER)
                self.assertEqual(events, [])
                self.assertEqual(expired["seen"], {})
                self.pages = [
                    [
                        pull(
                            state="closed",
                            merged_at=merged,
                            closed_at=OLDER,
                            created_at=OLDER,
                            body="补充说明",
                            updated_at="2026-11-16T08:02:00Z",
                        )
                    ]
                ]
                state, events = await self.pulls.collect(
                    REPO, expired, "2026-11-16T08:02:00Z"
                )
                self.assertEqual(len(events), 1)
                self.assertTrue(events[0].title.startswith("活动更新 PR #1"))
                self.assertEqual(
                    (await self.pulls.collect(REPO, state, "2026-11-16T08:02:00Z"))[1],
                    [],
                )

    async def test_recent_transition_on_previously_unseen_old_pr_is_labeled(self):
        self.pages = [[]]
        old = await self.baseline()
        for changes, label in (
            ({"state": "closed", "closed_at": LATER}, "关闭"),
            ({"state": "closed", "closed_at": LATER, "merged_at": LATER}, "合并"),
        ):
            with self.subTest(label=label):
                self.pages = [[pull(created_at=OLDER, updated_at=LATER, **changes)]]
                _, events = await self.pulls.collect(REPO, old, LATER)
                self.assertEqual(len(events), 1)
                self.assertTrue(events[0].title.startswith(f"{label} PR #1"))

    async def test_pagination_duplicate_conflict_and_missing_anchor_do_not_advance(
        self,
    ):
        old = await self.baseline()
        original = copy.deepcopy(old)
        for pages in (
            [[pull()], [pull()]],
            [[pull()], [pull(title="different")]],
            [[pull(2)]],
            [[pull(updated_at=OLDER)], [pull()]],
        ):
            with self.subTest(pages=pages):
                self.pages = pages
                with self.assertRaises(GitHubError):
                    await self.pulls.collect(REPO, old, LATER)
                self.assertEqual(old, original)

    async def test_page_limit_and_network_failure_preserve_old(self):
        old = await self.baseline()
        original = copy.deepcopy(old)
        self.client.settings.max_pages = 1
        self.pages = [[pull()], [pull(2)]]
        with self.assertRaisesRegex(GitHubError, "max_pages"):
            await self.pulls.collect(REPO, old, LATER)
        self.assertEqual(old, original)
        self.client.settings.max_pages = 3
        self.pages = [[pull()], GitHubError("network failure")]
        with self.assertRaisesRegex(GitHubError, "network failure"):
            await self.pulls.collect(REPO, old, LATER)
        self.assertEqual(old, original)

    async def test_invalid_numbers_dates_shas_and_order_are_rejected(self):
        for data in (
            pull(number=True),
            pull(number=-1),
            pull(updated_at="yesterday"),
            pull(updated_at="2026-11-16T08:00:00"),
            pull(head={"sha": "wrong"}),
            pull(state="unexpected"),
            pull(merged_at=DATE),
            pull(body={"bad": "body"}),
        ):
            with self.subTest(data=data):
                self.pages = [[data]]
                with self.assertRaises(GitHubError):
                    await self.pulls.collect(REPO, None, DATE)
        self.pages = [[pull(), pull(2, updated_at=LATER)]]
        with self.assertRaisesRegex(GitHubError, "顺序"):
            await self.pulls.collect(REPO, None, DATE)

    async def test_notifications_redact_credentials_and_do_not_include_patch(self):
        old = await self.baseline()
        secret = "private-test-secret ghp_abcdefg Bearer another-secret"
        self.pages = [[pull(title=secret, body=secret + "\x00", patch="never include")]]
        _, events = await self.pulls.collect(REPO, old, DATE)
        encoded = json.dumps(asdict(events[0]), ensure_ascii=False)
        for value in (
            "private-test-secret",
            "ghp_abcdefg",
            "another-secret",
            "never include",
        ):
            self.assertNotIn(value, encoded)
        self.assertNotIn("\x00", events[0].body)


class PullContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = GitHubClient(
            Settings(github_owner="test-owner", github_token="private-test-secret")
        )
        self.pulls = PullRequests(self.client)
        self.repository = copy.deepcopy(REPO)
        self.detail = pull(changed_files=1)
        self.event = await pull_event(self.detail)
        self.final_detail = None
        self.final_repository = None
        self.file_pages = [[file_item()]]
        self.link_override = None
        self.detail_reads = 0
        self.repository_reads = 0
        self.calls = []
        self.client._request = AsyncMock(side_effect=self.request)

    async def asyncTearDown(self):
        await self.client.close()

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        self.assertEqual(method, "GET")
        self.assertFalse(kwargs)
        parts = urlsplit(url)
        self.assertIn(parts.netloc, ("", "api.github.com"))
        if parts.path == "/repos/test-owner/demo":
            self.repository_reads += 1
            data = (
                self.final_repository
                if self.repository_reads > 1 and self.final_repository is not None
                else self.repository
            )
            return copy.deepcopy(data), {}
        if parts.path == PATH + "/1":
            self.detail_reads += 1
            data = (
                self.final_detail
                if self.detail_reads > 1 and self.final_detail is not None
                else self.detail
            )
            return copy.deepcopy(data), {}
        if parts.path == PATH + "/1/files":
            query = parse_qs(parts.query)
            self.assertEqual(query["per_page"], ["20"])
            page = int(query.get("page", ["1"])[0])
            headers = {}
            if page < len(self.file_pages):
                next_url = url.split("&page=", 1)[0] + f"&page={page + 1}"
                headers["link"] = f'<{next_url}>; rel="next"'
            if self.link_override is not None:
                headers["link"] = self.link_override
            return copy.deepcopy(self.file_pages[page - 1]), headers
        raise AssertionError(parts.path)

    async def test_whitelisted_context_has_patch_scope_and_version_rechecks(self):
        data = await self.pulls.context(self.event)
        self.assertEqual(
            set(data),
            {
                "url",
                "number",
                "head_sha",
                "base_sha",
                "description",
                "state",
                "scope",
                "files",
                "truncated",
                "missing_patch",
                "limitation",
            },
        )
        self.assertEqual(data["scope"], "当前PR累计差异")
        self.assertEqual(data["head_sha"], "a" * 40)
        self.assertEqual(data["base_sha"], "b" * 40)
        self.assertEqual(data["files"][0]["patch"], file_item()["patch"])
        self.assertNotIn("raw_url", data["files"][0])
        self.assertNotIn("blob_url", data["files"][0])
        self.assertFalse(data["truncated"])
        self.assertFalse(data["missing_patch"])
        self.assertEqual(self.detail_reads, 2)
        self.assertEqual(self.repository_reads, 2)
        self.assertEqual(len(self.calls), 5)

    async def test_invalid_event_link_owner_and_key_rejected_before_request(self):
        cases = [
            {"kind": "commit"},
            {"repo": "other-owner/demo"},
            {"repo": "test-owner/../demo"},
            {"url": "https://evil.example/pull/1"},
            {"url": "https://github.com/test-owner/demo/pull/2"},
            {"url": self.event["url"] + "?redirect=evil"},
            {"url": self.event["url"] + "#fragment"},
            {"event_key": "github:1:pull:1:bad"},
            {"event_key": self.event["event_key"] + ":extra"},
            {"event_key": self.event["event_key"].replace("pull:1:", "pull:2:")},
        ]
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaises(GitHubError):
                    await self.pulls.context({**self.event, **change})
        self.client.settings.excluded_repositories = "demo"
        with self.assertRaises(GitHubError):
            await self.pulls.context(self.event)
        self.assertEqual(self.calls, [])

    async def test_changed_head_or_base_rejected_before_files(self):
        for key in ("head", "base"):
            with self.subTest(key=key):
                self.detail = pull(**{key: {"sha": "c" * 40}})
                with self.assertRaisesRegex(GitHubError, "SHA 已变化"):
                    await self.pulls.context(self.event)
        self.assertFalse(any("/files" in call[1] for call in self.calls))

    async def test_force_push_during_files_is_rejected_by_second_detail(self):
        self.final_detail = pull(head={"sha": "c" * 40})
        with self.assertRaisesRegex(GitHubError, "SHA 已变化"):
            await self.pulls.context(self.event)
        self.assertEqual(self.detail_reads, 2)
        self.assertTrue(any("/files" in call[1] for call in self.calls))

    async def test_metadata_change_after_queue_is_rejected_before_files(self):
        for change in (
            {"title": "later title"},
            {"body": "later description"},
            {"state": "closed"},
            {"state": "closed", "merged_at": LATER},
            {"draft": True},
            {"updated_at": LATER},
        ):
            with self.subTest(change=change):
                self.detail = pull(changed_files=1, **change)
                self.calls.clear()
                with self.assertRaisesRegex(GitHubError, "版本信息已变化"):
                    await self.pulls.context(self.event)
                self.assertFalse(any("/files" in call[1] for call in self.calls))

    async def test_metadata_change_during_files_is_rejected_after_read(self):
        for change in ({"body": "later description"}, {"state": "closed"}):
            with self.subTest(change=change):
                self.final_detail = pull(changed_files=1, **change)
                self.calls.clear()
                self.detail_reads = 0
                with self.assertRaisesRegex(GitHubError, "版本信息已变化"):
                    await self.pulls.context(self.event)
                self.assertEqual(self.detail_reads, 2)
                self.assertTrue(any("/files" in call[1] for call in self.calls))

    async def test_repo_id_number_and_base_identity_must_match(self):
        for changes in (
            {"number": 2},
            {"base": {"sha": "b" * 40, "repo": {"full_name": "test-owner/other"}}},
            {"base": {"sha": "b" * 40, "repo": {**REPO, "id": 2}}},
        ):
            with self.subTest(changes=changes):
                self.detail = pull(**changes)
                with self.assertRaises(GitHubError):
                    await self.pulls.context(self.event)
        self.detail = pull()
        self.repository["id"] = 2
        with self.assertRaisesRegex(GitHubError, "仓库身份"):
            await self.pulls.context(self.event)

    async def test_public_to_private_and_unknown_visibility_are_blocked(self):
        for private in (True, None, "false"):
            with self.subTest(private=private):
                self.repository["private"] = private
                with self.assertRaisesRegex(GitHubError, "公开仓库"):
                    await self.pulls.context(self.event)
        self.assertFalse(any("/files" in call[1] for call in self.calls))
        self.repository["private"] = False
        self.final_repository = {**REPO, "private": True}
        with self.assertRaisesRegex(GitHubError, "公开仓库"):
            await self.pulls.context(self.event)

    async def test_explicit_private_opt_in_allows_private_repository(self):
        self.client.settings.ai_summary_private = True
        self.repository["private"] = True
        context = await self.pulls.context(self.event)
        self.assertEqual(len(context["files"]), 1)
        self.assertEqual(self.repository_reads, 1)
        self.assertEqual(self.detail_reads, 2)

    async def test_missing_patch_long_patch_and_credentials_are_explicit(self):
        credentials = "private-test-secret ghp_abcdefg Bearer another-secret"
        self.detail["body"] = credentials
        self.detail["changed_files"] = 2
        self.event = await pull_event(self.detail)
        self.file_pages = [
            [
                file_item("image.png", patch=None),
                file_item("src/a.py", patch=credentials + "\x00\n" + "x" * 3000),
            ]
        ]
        data = await self.pulls.context(self.event)
        encoded = json.dumps(data, ensure_ascii=False)
        for value in (
            "private-test-secret",
            "ghp_abcdefg",
            "another-secret",
            "evil.example",
        ):
            self.assertNotIn(value, encoded)
        self.assertTrue(data["files"][0]["missing_patch"])
        self.assertEqual(data["files"][0]["patch"], "")
        self.assertTrue(data["files"][1]["truncated"])
        self.assertLessEqual(len(data["files"][1]["patch"]), 1800)
        self.assertTrue(data["missing_patch"])
        self.assertTrue(data["truncated"])
        self.assertIn("二进制", data["limitation"])
        self.assertIn("部分内容", data["limitation"])

    async def test_total_json_and_file_count_are_bounded(self):
        self.detail["changed_files"] = 50
        self.file_pages = [
            [file_item(f"src/{i}.py", patch='\\"' * 2000) for i in range(25)]
        ]
        data = await self.pulls.context(self.event)
        self.assertLessEqual(len(data["files"]), 20)
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False)), 16000)
        self.assertTrue(data["truncated"])

    async def test_file_page_cap_and_reported_count_mark_incomplete(self):
        self.detail["changed_files"] = 10
        self.file_pages = [[file_item(f"src/{i}.py")] for i in range(5)]
        for limit in (1, 3, 20):
            with self.subTest(limit=limit):
                self.client.settings.max_pages = limit
                self.calls.clear()
                data = await self.pulls.context(self.event)
                self.assertEqual(len(data["files"]), min(limit, 3))
                self.assertTrue(data["truncated"])
                self.assertEqual(
                    sum("/files" in call[1] for call in self.calls), min(limit, 3)
                )
        self.file_pages = [[file_item()]]
        data = await self.pulls.context(self.event)
        self.assertTrue(data["truncated"])
        self.assertIn("部分内容", data["limitation"])

    async def test_twenty_file_limit_stops_before_unneeded_page(self):
        self.detail["changed_files"] = 21
        self.file_pages = [
            [file_item(f"src/{i}.py") for i in range(20)],
            [file_item("extra")],
        ]
        data = await self.pulls.context(self.event)
        self.assertEqual(len(data["files"]), 20)
        self.assertTrue(data["truncated"])
        self.assertEqual(sum("/files" in call[1] for call in self.calls), 1)

    async def test_external_or_wrong_resource_file_pagination_is_rejected(self):
        for target in (
            "https://evil.example/files?per_page=20&page=2",
            API + "/repos/other-owner/demo/pulls/1/files?per_page=20&page=2",
            API + PATH + "/2/files?per_page=20&page=2",
            API + PATH + "/1/files?per_page=20&page=3",
        ):
            with self.subTest(target=target):
                self.link_override = f'<{target}>; rel="next"'
                with self.assertRaises(GitHubError):
                    await self.pulls.context(self.event)
        self.assertFalse(any("evil.example" in call[1] for call in self.calls))

    async def test_invalid_file_data_duplicate_and_count_conflict_are_rejected(self):
        for files in (
            [file_item(additions=True)],
            [file_item(patch={"invalid": True})],
            [file_item(), file_item()],
            [file_item(), file_item("another.py")],
        ):
            with self.subTest(files=files):
                self.file_pages = [files]
                with self.assertRaises(GitHubError):
                    await self.pulls.context(self.event)
