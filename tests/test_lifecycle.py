import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from github_bridge.service import BridgeService
from github_bridge.settings import SettingsStore
from github_bridge.storage import Store
from tests.test_service_web import FakeGitHub


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminate_cancels_task_and_closes_client_and_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = SettingsStore(root)
            store = Store(root / "state.sqlite3")
            sender = AsyncMock()
            service = BridgeService(settings, store, sender, FakeGitHub)
            await service.start()
            task = service._task
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            await service.close()
            self.assertTrue(task.done())
            self.assertTrue(service.client.closed)
            self.assertFalse(service.running)
            sender.assert_not_awaited()
            with self.assertRaises(sqlite3.ProgrammingError):
                store.db.execute("SELECT 1")
