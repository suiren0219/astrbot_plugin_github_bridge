import tempfile
import unittest
from pathlib import Path

from github_bridge.migration import parse_legacy
from github_bridge.models import Event, Observation
from github_bridge.settings import Settings
from github_bridge.storage import Store


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings()
        self.sha = "a" * 40
        self.sent = {"suiren0219/gdust-v2/feature/test": {self.sha: [["123"]]}}
        self.failed = [{"repo_info": {"owner": {"login": "suiren0219"}, "name": "gdust-v2"},
                        "new_commits": [{"sha": self.sha, "message": "修复导入", "url": "https://untrusted.example"}],
                        "targets": ["1906986796"], "group_targets": ["123"], "attempts": 1}]

    def test_legacy_keys_with_slash_branch_and_owner_filter(self):
        data = {**self.sent, "thirdparty/demo": {self.sha: [[]]}}
        result = parse_legacy(data, None, self.cfg)
        self.assertEqual(result.seen, [f"suiren0219/gdust-v2@{self.sha}"])
        self.assertEqual(result.skipped, 1)

    def test_failed_private_delivery_wins_over_old_sent_and_reimport_is_idempotent(self):
        result = parse_legacy(self.sent, self.failed, self.cfg)
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state.sqlite3")
            try:
                self.assertEqual(store.import_records(self.cfg.identity, self.cfg.target_umo, result.seen, result.failed), (1, 1))
                self.assertEqual(store.import_records(self.cfg.identity, self.cfg.target_umo, result.seen, result.failed), (0, 0))
                queue = store.queue(self.cfg.identity, self.cfg.target_umo, 5)
                self.assertEqual(len(queue), 1)
                self.assertEqual(queue[0]["url"], f"https://github.com/suiren0219/gdust-v2/commit/{self.sha}")
                repo = {"id": 1, "full_name": "suiren0219/gdust-v2"}
                store.discover(self.cfg.identity, [repo])
                event = Event("new-event-key", repo["full_name"], "commit", "title", "body", "", "", (f"suiren0219/gdust-v2@{self.sha}",))
                self.assertEqual(store.record(self.cfg.identity, self.cfg.target_umo, repo, Observation({}, [event])), 0)
            finally:
                store.close()

    def test_group_only_and_other_private_target_are_not_queued(self):
        self.failed[0]["targets"] = ["other-admin"]
        result = parse_legacy(None, self.failed, self.cfg)
        self.assertEqual(result.failed, [])
        self.assertEqual(result.skipped, 1)
        self.failed[0]["targets"] = []
        self.assertEqual(parse_legacy(None, self.failed, self.cfg).failed, [])

    def test_unrecognized_format_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_legacy([], None, self.cfg)
        with self.assertRaises(ValueError):
            parse_legacy(None, {}, self.cfg)
        with self.assertRaises(ValueError):
            parse_legacy(None, None, self.cfg)
