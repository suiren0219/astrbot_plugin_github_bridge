import asyncio
import unittest

from github_bridge.service import (
    MAX_NOTIFICATION_CHARS,
    BridgeService,
)


class EventFormatTests(unittest.TestCase):
    def event(self, title, body, kind="commit"):
        return {
            "kind": kind,
            "repo": "test-owner/demo",
            "title": title,
            "body": body,
            "url": "https://github.com/test-owner/demo/commit/" + "a" * 40,
        }

    def test_single_line_commit_subject_appears_once_without_an_empty_body_line(self):
        title = "fix: 修复通知标题重复"
        event = self.event(title, title)
        original = event.copy()

        message = BridgeService.format_event(event)

        self.assertEqual(
            message.splitlines(),
            ["[GitHub · 代码提交]", event["repo"], title, event["url"]],
        )
        self.assertEqual(event, original)

    def test_multiline_commit_preserves_details_and_later_mentions_of_the_subject(self):
        title = "fix: 修复通知标题重复"
        detail = f"保留详细说明。\n\n相关提交：\n{title}\n- 保留列表缩进"
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                event = self.event(title, title + newline + newline + detail)
                self.assertEqual(
                    BridgeService.format_event(event),
                    f"[GitHub · 代码提交]\n{event['repo']}\n{title}\n{detail}\n{event['url']}",
                )

    def test_subject_is_compared_before_display_truncation(self):
        title = "修" * 240
        event = self.event(title, title + "\n\n详细说明")
        self.assertEqual(
            BridgeService.format_event(event).splitlines(),
            [
                "[GitHub · 代码提交]",
                event["repo"],
                title[:200],
                "详细说明",
                event["url"],
            ],
        )

    def test_collector_truncated_subject_is_not_repeated_in_body(self):
        subject = "修" * 350
        title = subject[:299] + "…"
        event = self.event(title, subject + "\n\n详细说明")
        self.assertEqual(
            BridgeService.format_event(event).splitlines(),
            [
                "[GitHub · 代码提交]",
                event["repo"],
                title[:200],
                "详细说明",
                event["url"],
            ],
        )

    def test_different_body_first_line_is_not_removed_by_prefix_match(self):
        for title, body in (
            ("fix: 修复", "fix: 修复详情，正文必须保留"),
            ("fix: 等待…", "fix: 等待更多信息"),
            ("A" * 299 + "…", "A" * 298 + "B" * 80),
        ):
            with self.subTest(title=title):
                event = self.event(title, body)
                self.assertEqual(
                    BridgeService.format_event(event).splitlines(),
                    [
                        "[GitHub · 代码提交]",
                        event["repo"],
                        title[:200],
                        body,
                        event["url"],
                    ],
                )

    def test_empty_commit_body_is_omitted(self):
        for body in ("", "\n\n", "  \r\n"):
            with self.subTest(body=repr(body)):
                event = self.event("fix: 修复", body)
                self.assertEqual(
                    BridgeService.format_event(event).splitlines(),
                    [
                        "[GitHub · 代码提交]",
                        event["repo"],
                        event["title"],
                        event["url"],
                    ],
                )

    def test_non_commit_events_keep_their_body_even_when_matching_the_title(self):
        for kind, label in (("release", "新 Release"), ("issue", "Issue 动态")):
            with self.subTest(kind=kind):
                event = self.event("说明", "说明\n详细内容", kind)
                self.assertEqual(
                    BridgeService.format_event(event),
                    f"[GitHub · {label}]\n{event['repo']}\n说明\n说明\n详细内容\n{event['url']}",
                )

    def test_body_limit_applies_to_details_after_subject_is_removed(self):
        title = "fix: 修复通知"
        detail = "内容" * 800
        event = self.event(title, title + "\n\n" + detail)
        self.assertEqual(
            BridgeService.format_event(event).splitlines(),
            ["[GitHub · 代码提交]", event["repo"], title, detail[:1400], event["url"]],
        )

    def test_batches_preserve_order_and_respect_message_length(self):
        service = object.__new__(BridgeService)
        events = []
        for index in range(7):
            events.append(
                {
                    "id": index + 1,
                    "repo": "test-owner/demo",
                    "kind": "commit",
                    "title": f"提交 {index}",
                    "body": f"说明 {index}",
                    "url": f"https://github.com/test-owner/demo/commit/{index}",
                }
            )

        async def summarize_event(event):
            return f"摘要 {event['id']}"

        service.summarize_event = summarize_event

        batches = asyncio.run(service._notification_batches(events))
        self.assertEqual(
            [event["id"] for _, group in batches for event in group],
            list(range(1, 8)),
        )
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0][0]), 1)
        self.assertLessEqual(len(batches[0][0][0]), MAX_NOTIFICATION_CHARS)
        for index in range(7):
            self.assertIn(f"摘要 {index + 1}", batches[0][0][0])

    def test_event_groups_are_split_at_character_budget(self):
        service = object.__new__(BridgeService)
        events = [
            {
                "id": index,
                "repo": "owner/demo",
                "kind": "release",
                "title": "更新 " + str(index),
                "body": "内容" * 700,
                "url": f"https://github.com/owner/demo/releases/{index}",
            }
            for index in range(3)
        ]

        async def run():
            async def summarize_event(event):
                return "摘要 " + str(event["id"])

            service.summarize_event = summarize_event
            return await service._notification_batches(events)

        batches = asyncio.run(run())
        self.assertGreater(len(batches), 1)
        self.assertEqual(
            [event["id"] for _, group in batches for event in group], [0, 1, 2]
        )
        self.assertTrue(
            all(
                len(part) <= MAX_NOTIFICATION_CHARS
                for parts, _ in batches
                for part in parts
            )
        )

    def test_oversized_single_event_splits_at_newline_without_loss(self):
        first = "头" * (MAX_NOTIFICATION_CHARS - 1) + "\n"
        second = "链接 https://github.com/owner/demo/commit/abc"
        pieces = BridgeService._split_notification(first + second)
        self.assertEqual(pieces, [first, second])
        self.assertTrue(all(len(piece) <= MAX_NOTIFICATION_CHARS for piece in pieces))

    def test_oversized_single_event_is_split_without_loss(self):
        original = "事件内容，含中文和 emoji 🐈\n" * 300
        pieces = BridgeService._split_notification(original)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= MAX_NOTIFICATION_CHARS for piece in pieces))
        self.assertEqual("".join(pieces), original)
