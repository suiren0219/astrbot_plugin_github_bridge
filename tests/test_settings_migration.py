import json
import tempfile
import unittest
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from github_bridge.settings import Settings, SettingsStore
from github_bridge.settings_migration import SETTINGS_SCHEMA_VERSION

# 测试使用通用旧预设夹具；运行代码的历史指纹另与旧提交核对。
LEGACY_PRESETS = {
    "github_owner": "old-demo-owner",
    "issue_repositories": "old-app,old-plugin-*",
    "target_umo": "old-bot:FriendMessage:123456",
}


class PresetCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "settings.json"
        self.env = patch.dict("os.environ", {"GITHUB_BRIDGE_TOKEN": ""})
        self.env.start()
        fingerprints = {
            key: sha256(value.encode("utf-8")).hexdigest()
            for key, value in LEGACY_PRESETS.items()
        }
        self.preset_patch = patch(
            "github_bridge.settings_migration.LEGACY_PRESET_DIGESTS", fingerprints
        )
        self.preset_patch.start()
        self.addCleanup(self.preset_patch.stop)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_old(self, **overrides):
        data = asdict(
            Settings(
                github_owner="custom-owner",
                github_token="fake-saved-token",
                target_umo="my-bot:FriendMessage:123456",
                issue_repositories="own-repo,own-plugin-*",
                excluded_repositories="private-demo",
                poll_interval=240,
                max_push_per_cycle=3,
                enabled=True,
            )
        )
        data.update(overrides)
        original = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.path.write_text(original, encoding="utf-8")
        return data, original

    def test_fresh_install_has_no_personal_presets_or_issue_grants(self):
        store = SettingsStore(self.root)
        self.assertEqual(store.value.github_owner, "")
        self.assertEqual(store.value.target_umo, "")
        self.assertEqual(store.value.issue_repositories, "")
        self.assertFalse(store.value.enabled)
        self.assertFalse(store.value.allows_issue("test-owner/repo-name"))
        self.assertEqual(
            (store.value.poll_interval, store.value.max_push_per_cycle), (120, 5)
        )
        self.assertEqual(store.configuration_warning, "")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["_schema_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(
            list(self.root.glob("settings.before-preset-cleanup.*.json")), []
        )

    def test_each_exact_legacy_preset_is_cleared_and_backed_up(self):
        for field, value in LEGACY_PRESETS.items():
            with self.subTest(field=field):
                data, original = self.write_old(**{field: value})
                store = SettingsStore(self.root)
                self.assertEqual(getattr(store.value, field), "")
                self.assertFalse(store.value.enabled)
                for key in ("github_owner", "issue_repositories", "target_umo"):
                    if key != field:
                        self.assertEqual(getattr(store.value, key), data[key])
                self.assertEqual(store.value.github_token, data["github_token"])
                self.assertEqual(store.value.excluded_repositories, "private-demo")
                self.assertEqual(
                    (store.value.poll_interval, store.value.max_push_per_cycle),
                    (240, 3),
                )
                backups = list(self.root.glob("settings.before-preset-cleanup.*.json"))
                self.assertTrue(
                    any(p.read_text(encoding="utf-8") == original for p in backups)
                )
                self.assertNotIn("fake-saved-token", store.configuration_warning)
                self.assertIn("暂停监控", store.configuration_warning)

    def test_all_old_presets_are_cleared_without_touching_event_data(self):
        _, original = self.write_old(**LEGACY_PRESETS)
        marker = self.root / "bridge.sqlite3"
        marker.write_bytes(b"existing event data must remain untouched")
        store = SettingsStore(self.root)
        self.assertEqual(
            [getattr(store.value, key) for key in LEGACY_PRESETS], ["", "", ""]
        )
        self.assertFalse(store.value.enabled)
        self.assertEqual(
            marker.read_bytes(), b"existing event data must remain untouched"
        )
        backups = list(self.root.glob("settings.before-preset-cleanup.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_custom_configuration_is_preserved_and_needs_no_backup(self):
        data, _ = self.write_old()
        store = SettingsStore(self.root)
        self.assertEqual(asdict(store.value), data)
        self.assertTrue(store.value.enabled)
        self.assertEqual(store.configuration_warning, "")
        self.assertEqual(
            list(self.root.glob("settings.before-preset-cleanup.*.json")), []
        )

    def test_custom_allowlist_containing_old_text_is_not_rewritten(self):
        custom = "my-repo," + LEGACY_PRESETS["issue_repositories"]
        self.write_old(issue_repositories=custom)
        store = SettingsStore(self.root)
        self.assertEqual(store.value.issue_repositories, custom)
        self.assertTrue(store.value.enabled)

    def test_cleanup_is_once_only_and_explicit_values_survive_restart(self):
        self.write_old(**LEGACY_PRESETS)
        first = SettingsStore(self.root)
        self.assertTrue(first.configuration_warning)
        second = SettingsStore(self.root)
        self.assertTrue(second.configuration_warning)
        self.assertEqual(
            len(list(self.root.glob("settings.before-preset-cleanup.*.json"))), 1
        )
        second.update({**LEGACY_PRESETS, "enabled": True})
        third = SettingsStore(self.root)
        self.assertTrue(third.value.enabled)
        for key, value in LEGACY_PRESETS.items():
            self.assertEqual(getattr(third.value, key), value)
        self.assertEqual(third.configuration_warning, "")
        self.assertEqual(
            len(list(self.root.glob("settings.before-preset-cleanup.*.json"))), 1
        )

    def test_blank_configuration_can_be_saved_and_remains_blank_after_restart(self):
        store = SettingsStore(self.root)
        store.update(
            {
                "github_owner": "",
                "target_umo": "",
                "issue_repositories": "",
                "enabled": False,
            }
        )
        restored = SettingsStore(self.root)
        self.assertEqual(restored.value.github_owner, "")
        self.assertEqual(restored.value.target_umo, "")
        self.assertEqual(restored.value.issue_repositories, "")
        self.assertFalse(restored.value.enabled)

    def test_enable_requires_owner_token_and_target_but_not_issue_grants(self):
        store = SettingsStore(self.root)
        with self.assertRaises(ValueError):
            store.update({"enabled": True})
        store.update({"github_owner": "test-owner"})
        with self.assertRaises(ValueError):
            store.update({"enabled": True})
        store.update({"github_token": "fake-test-token"})
        with self.assertRaises(ValueError):
            store.update({"enabled": True})
        store.update({"target_umo": "my-bot:FriendMessage:123456", "enabled": True})
        self.assertTrue(store.value.enabled)
        self.assertEqual(store.value.issue_repositories, "")
        self.assertFalse(store.value.allows_issue("test-owner/repo-name"))

    def test_backup_failure_leaves_original_configuration_untouched(self):
        _, original = self.write_old(**LEGACY_PRESETS)
        with patch(
            "github_bridge.settings.secure_write",
            side_effect=OSError("backup unavailable"),
        ) as writer:
            with self.assertRaisesRegex(OSError, "backup unavailable"):
                SettingsStore(self.root)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertTrue(
            writer.call_args.args[0].name.startswith("settings.before-preset-cleanup.")
        )

    def test_environment_secret_is_not_copied_into_backup_or_configuration(self):
        self.write_old(**LEGACY_PRESETS)
        with patch.dict(
            "os.environ", {"GITHUB_BRIDGE_TOKEN": "fake-environment-token"}
        ):
            store = SettingsStore(self.root)
        self.assertEqual(store.value.github_token, "fake-environment-token")
        for path in self.root.glob("*.json"):
            self.assertNotIn("fake-environment-token", path.read_text(encoding="utf-8"))
        self.assertNotIn("fake-environment-token", json.dumps(store.value.public()))

    def test_saved_fields_acknowledge_warning_without_touching_other_fields(self):
        self.write_old(**LEGACY_PRESETS)
        store = SettingsStore(self.root)
        store.update({"enabled": False})
        self.assertEqual(set(store.cleared_legacy_presets), set(LEGACY_PRESETS))
        store.update({"issue_repositories": ""})
        self.assertNotIn("issue_repositories", store.cleared_legacy_presets)
        self.assertIn("github_owner", store.cleared_legacy_presets)

    def test_failed_save_keeps_migration_warning(self):
        self.write_old(**LEGACY_PRESETS)
        store = SettingsStore(self.root)
        before = list(store.cleared_legacy_presets)
        with patch(
            "github_bridge.settings.secure_write",
            side_effect=OSError("write unavailable"),
        ):
            with self.assertRaises(OSError):
                store.update({"github_owner": "test-owner"})
        self.assertEqual(store.value.github_owner, "")
        self.assertEqual(store.cleared_legacy_presets, before)
