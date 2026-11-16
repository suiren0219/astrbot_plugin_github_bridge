import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import package


class PackageTests(unittest.TestCase):
    def test_archive_keeps_runtime_views_and_excludes_local_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugin"
            root.mkdir()
            runtime = {
                "main.py",
                "metadata.yaml",
                "views/dashboard/index.html",
                "views/dashboard/app.js",
                ".astrbot-plugin/i18n/zh-CN.json",
            }
            excluded = {
                ".env",
                ".env.production",
                "settings.json",
                "panel.auth.json",
                "token.key",
                "cert.pem",
                "data.db",
                "bridge.sqlite3-wal",
                "build.zip",
                "tools/preview.py",
                "tests/test_fake.py",
                ".git/config",
                ".ruff_cache/cache",
                ".secrets/session",
            }
            for name in runtime | excluded:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            archive = Path(temp) / "plugin.zip"
            with patch.object(package, "ROOT", root):
                package.build(archive)
            with zipfile.ZipFile(archive) as result:
                self.assertIsNone(result.testzip())
                self.assertEqual(set(result.namelist()), runtime)
