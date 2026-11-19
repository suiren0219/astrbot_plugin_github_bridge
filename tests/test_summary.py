import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from github_bridge.models import Event, Observation
from github_bridge.service import BridgeService
from github_bridge.settings import Settings, SettingsStore
from github_bridge.storage import Store
from github_bridge.summary import AstrBotSummarizer, clean_summary, summary_prompt
from tests.test_service_web import FakeGitHub


class SummaryAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_provider_and_plain_text_request(self):
        context = SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(completion_text="修复导入校验。")
            ),
            get_current_chat_provider_id=AsyncMock(),
        )
        settings = Settings(ai_summary_provider_id="chosen-model")
        result = await AstrBotSummarizer(context)(
            {"kind": "commit", "title": "fix", "body": "校验更新"}, settings
        )
        self.assertEqual(result, "修复导入校验。")
        context.get_current_chat_provider_id.assert_not_awaited()
        kwargs = context.llm_generate.call_args.kwargs
        self.assertEqual(kwargs["chat_provider_id"], "chosen-model")
        self.assertNotIn("func_tool", kwargs)
        self.assertNotIn("contexts", kwargs)
        self.assertIn("不推测", kwargs["system_prompt"])

    async def test_default_provider_is_resolved_for_notification_target(self):
        context = SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(completion_text="发布更新。")
            ),
            get_current_chat_provider_id=AsyncMock(return_value="session-model"),
        )
        settings = Settings(target_umo="qq:FriendMessage:123")
        await AstrBotSummarizer(context)({"title": "发布"}, settings)
        context.get_current_chat_provider_id.assert_awaited_once_with(
            settings.target_umo
        )
        self.assertEqual(
            context.llm_generate.call_args.kwargs["chat_provider_id"], "session-model"
        )

    async def test_missing_provider_and_non_text_response_are_errors(self):
        context = SimpleNamespace(
            llm_generate=AsyncMock(),
            get_current_chat_provider_id=AsyncMock(return_value=None),
        )
        with self.assertRaises(ValueError):
            await AstrBotSummarizer(context)({}, Settings())
        context.llm_generate.assert_not_awaited()
        context.get_current_chat_provider_id.return_value = "model"
        context.llm_generate.return_value = SimpleNamespace(completion_text=None)
        with self.assertRaises(ValueError):
            await AstrBotSummarizer(context)({}, Settings())

    def test_prompt_is_bounded_and_secrets_are_removed(self):
        event = {
            "kind": "comment",
            "title": "title fake-secret",
            "body": "token=private-value\n" + "a" * 9000,
            "repo": "private/repo",
            "target": "qq:FriendMessage:123",
        }
        prompt = summary_prompt(event, "fake-secret")
        self.assertNotIn("fake-secret", prompt)
        self.assertNotIn("private-value", prompt)
        self.assertNotIn("private/repo", prompt)
        self.assertNotIn("FriendMessage", prompt)
        self.assertLess(len(prompt), 4500)
        self.assertIsInstance(json.loads(prompt.split("\n", 1)[1]), dict)

    def test_summary_is_bounded_and_reasoning_is_removed(self):
        result = clean_summary(
            "<think>不应推送的推理</think>\n实际总结 fake-secret token=private-value",
            "fake-secret",
        )
        self.assertNotIn("不应推送", result)
        self.assertNotIn("fake-secret", result)
        self.assertNotIn("private-value", result)
        self.assertLessEqual(len(clean_summary("字" * 500, "")), 240)
        for text in ("", "   ", "<think>只有推理", "<think>only</think>"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                clean_summary(text, "")


class SummaryQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = SettingsStore(
            self.root,
            {
                "github_owner": "test-owner",
                "github_token": "fake-secret",
                "target_umo": "qq:FriendMessage:123",
                "enabled": True,
            },
        )
        self.store = Store(self.root / "bridge.sqlite3")
        self.sender = AsyncMock()
        self.summarizer = AsyncMock(return_value="本次更新修复了数据导入校验。")
        self.service = BridgeService(
            self.settings,
            self.store,
            self.sender,
            FakeGitHub,
            summarizer=self.summarizer,
        )
        self.service.client.fail = True
        self.repo = {"id": 1, "full_name": "test-owner/demo", "private": False}

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def enqueue(self, key="event-1", repo=None):
        repo = repo or self.repo
        cfg = self.settings.value
        self.store.discover(cfg.identity, [repo])
        self.store.record(
            cfg.identity,
            cfg.target_umo,
            repo,
            Observation(
                {},
                [
                    Event(
                        key,
                        repo["full_name"],
                        "commit",
                        "修复导入校验",
                        "修复导入校验\n补充错误处理",
                        "https://github.com/test-owner/demo",
                        "",
                    )
                ],
            ),
        )
        return self.store.events(cfg.identity)[0]["id"]

    async def test_successful_summary_is_appended_and_original_link_remains(self):
        event_id = self.enqueue()
        await self.service.check()
        text = self.sender.call_args.args[1]
        self.assertIn("AI 总结：本次更新修复了数据导入校验。", text)
        self.assertIn("补充错误处理", text)
        self.assertTrue(text.endswith("https://github.com/test-owner/demo"))
        self.assertEqual(self.store.summary(event_id)["state"], "ready")
        self.assertEqual(self.store.counts("test-owner")["sent"], 1)
        self.summarizer.assert_awaited_once()

    async def test_send_retry_reuses_summary_after_database_reopen(self):
        event_id = self.enqueue()
        self.sender.side_effect = RuntimeError("QQ offline")
        await self.service.check()
        self.assertEqual(self.store.events("test-owner")[0]["status"], "failed")
        self.store.close()
        self.store = Store(self.root / "bridge.sqlite3")
        self.service.store = self.store
        self.store.retry("test-owner", self.settings.value.target_umo, event_id)
        self.sender.side_effect = None
        await self.service.check()
        self.summarizer.assert_awaited_once()
        self.assertEqual(self.sender.await_count, 2)
        self.assertEqual(self.store.events("test-owner")[0]["status"], "sent")

    async def test_model_failure_does_not_fail_notification_or_leak_provider_exception(
        self,
    ):
        event_id = self.enqueue()
        self.summarizer.side_effect = RuntimeError(
            "provider Authorization: fake-provider-secret"
        )
        await self.service.check()
        self.assertEqual(self.store.counts("test-owner")["sent"], 1)
        self.assertNotIn("AI 总结", self.sender.call_args.args[1])
        self.assertNotIn("fake-provider-secret", str(self.service.status()))
        self.assertEqual(self.store.summary(event_id)["state"], "failed")

    async def test_model_timeout_falls_back_and_send_retry_does_not_reinvoke_model(
        self,
    ):
        event_id = self.enqueue()

        async def slow(*args):
            raise asyncio.TimeoutError()

        self.summarizer.side_effect = slow
        self.sender.side_effect = RuntimeError("offline")
        await self.service.check()
        self.store.retry("test-owner", self.settings.value.target_umo, event_id)
        self.service._summary_cooldown = 0
        self.sender.side_effect = None
        await self.service.check()
        self.summarizer.assert_awaited_once()
        self.assertNotIn("AI 总结", self.sender.call_args.args[1])

    async def test_generation_is_actually_cancelled_when_timeout_expires(self):
        self.enqueue()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def never_finishes(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.summarizer.side_effect = never_finishes
        # 缩短测试时钟；生产配置仍限制为 3–60 秒。
        self.settings.value.ai_summary_timeout = 0.01
        await self.service.check()
        self.assertTrue(started.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.sender.await_count, 1)
        self.assertEqual(self.store.counts("test-owner")["sent"], 1)

    async def test_disabled_and_missing_model_do_not_invoke_summarizer(self):
        self.enqueue()
        self.settings.update({"ai_summary_enabled": False})
        await self.service.check()
        self.summarizer.assert_not_awaited()
        self.settings.update({"ai_summary_enabled": True})
        self.enqueue("event-2")
        self.service.summarizer = None
        await self.service.check()
        self.summarizer.assert_not_awaited()

    async def test_private_and_unknown_visibility_skip_model_by_default(self):
        for index, private in enumerate((True, None)):
            self.enqueue(f"private-{index}", {**self.repo, "private": private})
            await self.service.check()
        self.summarizer.assert_not_awaited()
        self.assertEqual(self.sender.await_count, 2)

    async def test_explicit_opt_in_allows_private_summary(self):
        self.settings.update({"ai_summary_private": True})
        self.enqueue(repo={**self.repo, "private": True})
        await self.service.check()
        self.summarizer.assert_awaited_once()
        self.assertIn("AI 总结", self.sender.call_args.args[1])

    async def test_disabling_summary_hides_cached_summary_on_retry(self):
        event_id = self.enqueue()
        self.sender.side_effect = RuntimeError("offline")
        await self.service.check()
        self.settings.update({"ai_summary_enabled": False})
        self.store.retry("test-owner", self.settings.value.target_umo, event_id)
        self.sender.side_effect = None
        await self.service.check()
        self.assertNotIn("AI 总结", self.sender.call_args.args[1])
        self.summarizer.assert_awaited_once()

    async def test_repo_becoming_private_does_not_reuse_public_summary(self):
        event_id = self.enqueue()
        self.sender.side_effect = RuntimeError("offline")
        await self.service.check()
        self.store.discover("test-owner", [{**self.repo, "private": True}])
        self.store.retry("test-owner", self.settings.value.target_umo, event_id)
        self.sender.side_effect = None
        await self.service.check()
        self.assertNotIn("AI 总结", self.sender.call_args.args[1])
        self.summarizer.assert_awaited_once()

    async def test_failure_circuit_does_not_block_remaining_queue(self):
        self.enqueue()
        self.enqueue("event-2")
        self.summarizer.side_effect = RuntimeError("offline")
        await self.service.check()
        self.summarizer.assert_awaited_once()
        self.assertEqual(self.sender.await_count, 1)
        message = self.sender.call_args.args[1]
        self.assertEqual(message.count("[GitHub · 代码提交]"), 2)

    async def test_multiple_summaries_are_kept_in_one_merged_message(self):
        first_id = self.enqueue("event-1")
        self.enqueue("event-2")
        self.summarizer.side_effect = ["第一条摘要", "第二条摘要"]

        await self.service.check()

        self.assertEqual(self.summarizer.await_count, 2)
        self.assertEqual(self.sender.await_count, 1)
        message = self.sender.call_args.args[1]
        self.assertLess(message.index("第一条摘要"), message.index("第二条摘要"))
        self.assertEqual(message.count("https://github.com/test-owner/demo"), 2)
        self.assertEqual(self.store.counts("test-owner")["sent"], 2)
        self.assertEqual(self.store.summary(first_id)["summary"], "第一条摘要")

    async def test_failed_merged_batch_retries_once_and_reuses_summaries(self):
        self.enqueue("event-1")
        self.enqueue("event-2")
        self.summarizer.side_effect = ["第一条摘要", "第二条摘要"]
        self.sender.side_effect = RuntimeError("QQ offline")

        await self.service.check()
        failed = self.store.events("test-owner", "failed")
        self.assertEqual(len(failed), 2)
        self.assertEqual(self.sender.await_count, 1)

        self.store.retry("test-owner", self.settings.value.target_umo)
        self.sender.side_effect = None
        await self.service.check()

        self.assertEqual(self.summarizer.await_count, 2)
        self.assertEqual(self.sender.await_count, 2)
        message = self.sender.call_args.args[1]
        self.assertIn("第一条摘要", message)
        self.assertIn("第二条摘要", message)
        self.assertEqual(self.store.counts("test-owner")["sent"], 2)

    async def test_successful_send_with_batch_mark_failure_is_not_auto_retried(self):
        self.enqueue("event-1")
        self.enqueue("event-2")
        original_mark_sent = self.store.mark_sent_many

        def fail_mark_sent(event_ids):
            return (_ for _ in ()).throw(RuntimeError("simulated sqlite commit error"))

        self.store.mark_sent_many = fail_mark_sent
        with self.assertRaisesRegex(RuntimeError, "本地发送状态记录失败"):
            await self.service.check()
        self.assertEqual(self.sender.await_count, 1)
        rows = self.store.events("test-owner", "failed")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(event["manual_retry"] for event in rows))

        self.store.mark_sent_many = original_mark_sent
        await self.service.check()
        self.assertEqual(self.sender.await_count, 1)
        rows = self.store.events("test-owner", "failed")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(event["manual_retry"] for event in rows))
        self.assertEqual(self.store.recover_interrupted(), 0)

    async def test_each_merged_group_is_accounted_independently(self):
        for index in range(5):
            self.enqueue(f"group-{index}")
        with self.store.db:
            self.store.db.execute(
                "UPDATE events SET body=? WHERE owner=?",
                ("详细内容" * 800, "test-owner"),
            )
        self.sender.side_effect = [None, RuntimeError("middle group failed"), None]

        await self.service.check()

        self.assertEqual(self.sender.await_count, 3)
        self.assertEqual(
            [
                call.args[1].count("[GitHub · 代码提交]")
                for call in self.sender.call_args_list
            ],
            [2, 2, 1],
        )
        rows = self.store.events("test-owner")
        self.assertEqual(sum(event["status"] == "sent" for event in rows), 3)
        self.assertEqual(sum(event["status"] == "failed" for event in rows), 2)

    async def test_split_event_partial_send_becomes_manual_only(self):
        self.enqueue("oversized")
        original = BridgeService.format_event

        def oversized(event, summary=""):
            return original(event, summary) + "详细内容。" * 1300

        self.service.format_event = oversized
        self.sender.side_effect = [None, RuntimeError("second part failed")]

        await self.service.check()

        self.assertEqual(self.sender.await_count, 2)
        failed = self.store.events("test-owner", "failed")
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["manual_retry"])
        self.assertIn("部分成功", failed[0]["last_error"])

    async def test_cancellation_during_sender_becomes_manual_only(self):
        self.enqueue("cancel-send")
        self.sender.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.service.check()

        self.sender.assert_awaited_once()
        failed = self.store.events("test-owner", "failed")
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["manual_retry"])
        self.assertIn("发送期间任务被取消", failed[0]["last_error"])
        self.assertFalse(self.service.running)

    async def test_cancellation_during_summary_keeps_event_pending(self):
        event_id = self.enqueue()
        self.summarizer.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.check()
        self.sender.assert_not_awaited()
        self.assertEqual(self.store.events("test-owner")[0]["status"], "pending")
        self.assertIsNone(self.store.summary(event_id))
        self.assertFalse(self.service.running)


class SummarySettingsTests(unittest.TestCase):
    def test_summary_settings_persist_without_token_exposure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SettingsStore(root)
            store.update(
                {
                    "ai_summary_enabled": False,
                    "ai_summary_provider_id": "test-provider",
                    "ai_summary_timeout": 25,
                    "ai_summary_private": True,
                }
            )
            restored = SettingsStore(root)
            self.assertFalse(restored.value.ai_summary_enabled)
            self.assertEqual(restored.value.ai_summary_provider_id, "test-provider")
            self.assertEqual(restored.value.ai_summary_timeout, 25)
            self.assertTrue(restored.value.ai_summary_private)
            self.assertNotIn("github_token", restored.value.public())

    def test_summary_setting_types_and_timeout_bounds(self):
        for patch in (
            {"ai_summary_enabled": "yes"},
            {"ai_summary_private": 1},
            {"ai_summary_provider_id": []},
            {"ai_summary_timeout": 2},
            {"ai_summary_timeout": 61},
            {"ai_summary_timeout": True},
        ):
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as temp:
                store = SettingsStore(Path(temp))
                with self.assertRaises(ValueError):
                    store.update(patch)


class SummaryStorageUpgradeTests(unittest.TestCase):
    def test_upgrade_old_database_preserves_existing_notification(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            old = sqlite3.connect(path)
            old.execute(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            old.execute("INSERT INTO metadata VALUES('fixture','true')")
            old.commit()
            old.close()
            store = Store(path)
            try:
                self.assertTrue(store.meta("fixture"))
                self.assertIsNone(store.summary(1))
                store.save_summary(1, "摘要", "ready")
                self.assertEqual(store.summary(1)["summary"], "摘要")
            finally:
                store.close()

    def test_existing_queue_with_unknown_visibility_is_not_sent_to_model(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            repo = {"id": 1, "full_name": "test-owner/demo", "private": False}
            store = Store(path)
            store.discover("test-owner", [repo])
            store.record(
                "test-owner",
                "qq:FriendMessage:123",
                repo,
                Observation(
                    {},
                    [
                        Event(
                            "old", repo["full_name"], "commit", "title", "body", "", ""
                        )
                    ],
                ),
            )
            store.db.execute("DROP TABLE event_visibility")
            store.db.execute("DROP TABLE event_summaries")
            store.db.commit()
            store.close()
            upgraded = Store(path)
            try:
                event = upgraded.queue("test-owner", "qq:FriendMessage:123", 5)[0]
                self.assertEqual(event["title"], "title")
                self.assertFalse(upgraded.is_public_event(event, "test-owner"))
                self.assertIsNone(upgraded.summary(event["id"]))
            finally:
                upgraded.close()
