import tempfile
import unittest
from pathlib import Path

from github_bridge.models import Event, Observation
from github_bridge.settings import Settings, SettingsStore
from github_bridge.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bridge.sqlite3"
        self.store = Store(self.path)
        self.repo = {"id": 1, "full_name": "owner/demo", "private": False}
        self.target = "platform:FriendMessage:1"
        self.store.discover("owner", [self.repo])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, key="commit:abc", **kw):
        return Event(key, "owner/demo", "commit", "提交", "详情", "https://github.com/owner/demo", "2026-11-16T00:00:00Z", **kw)

    def test_baseline_snapshot_and_dedup_persist_after_restart(self):
        self.store.record("owner", self.target, self.repo, Observation({"head": "old"}))
        self.assertEqual(self.store.queue("owner", self.target, 5), [])
        observation = Observation({"head": "new"}, [self.event()])
        self.assertEqual(self.store.record("owner", self.target, self.repo, observation), 1)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.record("owner", self.target, self.repo, observation), 0)
        self.assertEqual(self.store.snapshot("owner", 1)["head"], "new")
        self.assertEqual(len(self.store.queue("owner", self.target, 5)), 1)

    def test_outbox_and_snapshot_are_atomic(self):
        self.store.record("owner", self.target, self.repo, Observation({"head": "old"}))
        with self.assertRaises(TypeError):
            self.store.record("owner", self.target, self.repo, Observation({"invalid": object()}, [self.event()]))
        self.assertEqual(self.store.snapshot("owner", 1), {"head": "old"})
        self.assertEqual(self.store.queue("owner", self.target, 5), [])

    def test_only_current_target_and_owner_can_send(self):
        self.store.record("owner", self.target, self.repo, Observation({}, [self.event()]))
        self.assertEqual(self.store.queue("other", self.target, 5), [])
        self.assertEqual(self.store.queue("owner", "platform:FriendMessage:2", 5), [])

    def test_failure_backoff_manual_retry_and_interrupted_delivery(self):
        self.store.record("owner", self.target, self.repo, Observation({}, [self.event()]))
        event_id = self.store.queue("owner", self.target, 5)[0]["id"]
        self.store.mark_sending(event_id)
        self.store.mark_failed(event_id, "offline")
        self.assertEqual(self.store.queue("owner", self.target, 5), [])
        self.assertEqual(self.store.retry("owner", self.target), 1)
        self.store.mark_sending(event_id)
        self.assertEqual(self.store.recover_interrupted(), 1)
        self.assertEqual(self.store.queue("owner", self.target, 5), [])
        self.store.retry("owner", self.target, event_id)
        self.store.mark_sent(event_id)
        self.assertEqual(self.store.retry("owner", self.target), 0)
        self.assertEqual(self.store.counts("owner")["sent"], 1)

    def test_legacy_alias_suppresses_future_duplicates(self):
        self.store.import_records("owner", self.target, ["owner/demo:abc"], [])
        event = self.event(legacy_keys=("owner/demo:abc",))
        self.assertEqual(self.store.record("owner", self.target, self.repo, Observation({}, [event])), 0)

    def test_reset_baseline_keeps_outbox_and_is_owner_scoped(self):
        self.store.record("owner", self.target, self.repo, Observation({"head": "old"}, [self.event()]))
        self.assertFalse(self.store.reset_snapshot("other", "owner/demo"))
        self.assertTrue(self.store.reset_snapshot("owner", "owner/demo"))
        self.assertIsNone(self.store.snapshot("owner", 1))
        self.assertEqual(len(self.store.queue("owner", self.target, 5)), 1)

    def test_cross_instance_lease(self):
        other = Store(self.path)
        try:
            self.assertTrue(self.store.acquire("a"))
            self.assertFalse(other.acquire("b"))
            self.store.release("a")
            self.assertTrue(other.acquire("b"))
        finally:
            other.close()

    def test_rename_preserves_snapshot_and_absent_repos_are_inactive(self):
        self.store.record("owner", self.target, self.repo, Observation({"head": "abc"}))
        self.store.discover("owner", [{"id": 1, "full_name": "owner/new"}])
        self.assertEqual(self.store.snapshot("owner", 1)["head"], "abc")
        self.assertEqual(self.store.repositories("owner")[0]["full_name"], "owner/new")
        self.store.discover("owner", [])
        self.assertEqual(self.store.repositories("owner"), [])


class SettingsTests(unittest.TestCase):
    def test_target_can_only_be_private_and_bool_is_not_integer(self):
        with self.assertRaises(ValueError):
            Settings(target_umo="platform:GroupMessage:123").validate()
        with self.assertRaises(ValueError):
            Settings(max_push_per_cycle=True).validate()

    def test_repo_allowlist_is_owner_scoped(self):
        settings = Settings()
        self.assertTrue(settings.allows_issue("suiren0219/gdust-v2"))
        self.assertTrue(settings.allows_issue("suiren0219/astrbot_plugin_demo"))
        self.assertFalse(settings.allows_issue("evil/gdust-v2"))
        self.assertFalse(settings.allows_issue("suiren0219/GKDX"))

    def test_token_is_write_only_and_panel_key_is_hashed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            store.update({"github_token": "sample-secret"})
            store.update({"github_token": "", "enabled": True})
            self.assertEqual(store.value.github_token, "sample-secret")
            self.assertNotIn("sample-secret", str(store.value.public()))
            store.set_panel_key("a-long-panel-password")
            self.assertNotIn("a-long-panel-password", store.auth_path.read_text())
            self.assertTrue(store.verify_panel_key("a-long-panel-password"))
            self.assertFalse(store.verify_panel_key("incorrect"))
            with self.assertRaises(ValueError):
                store.update({"clear_token": True})
            self.assertTrue(store.value.enabled)
