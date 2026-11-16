import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from github_bridge.panel import MAX_BODY_BYTES, PanelAPI


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = SimpleNamespace(
            username="admin", body=AsyncMock(return_value=b"{}")
        )
        self.web = SimpleNamespace(
            request=self.request,
            json_response=lambda data, status_code=200: (status_code, data),
            error_response=lambda message, status_code=400: (
                status_code,
                {"error": message},
            ),
        )
        self.service = Mock()
        self.service.safe_error = str
        self.panel = PanelAPI(self.service, web_api=self.web)

    async def test_missing_dashboard_identity_is_rejected_before_business_call(self):
        self.request.username = None
        status, _ = await self.panel.dispatch("GET", "status")
        self.assertEqual(status, 401)
        self.service.status.assert_not_called()

    async def test_request_size_and_json_shape_are_validated_in_production_adapter(
        self,
    ):
        for raw in (b"x" * (MAX_BODY_BYTES + 1), b"invalid json", b"[]"):
            with self.subTest(size=len(raw)):
                self.request.body.return_value = raw
                status, body = await self.panel.dispatch("POST", "settings")
                self.assertEqual(status, 400)
                self.assertIn("error", body)
        self.service.update_settings.assert_not_called()

    async def test_valid_payload_calls_service_and_never_returns_token(self):
        self.request.body.return_value = json.dumps({"poll_interval": 180}).encode()
        self.service.update_settings = AsyncMock(
            return_value={"poll_interval": 180, "token_configured": True}
        )
        status, body = await self.panel.dispatch("POST", "settings")
        self.assertEqual(status, 200)
        self.assertTrue(body["token_configured"])
        self.service.update_settings.assert_awaited_once_with({"poll_interval": 180})

    async def test_check_response_includes_completion_baseline(self):
        self.service.last_check = 100.0
        status, body = await self.panel.dispatch("POST", "check")
        self.assertEqual(status, 200)
        self.assertEqual(body["last_check"], 100.0)
        self.service.request_check.assert_called_once()

    async def test_import_requires_explicit_target_before_loading_history(self):
        self.service.running = False
        self.service.settings.value.target_umo = ""
        self.request.body.return_value = b'{"sent":{},"failed":[]}'
        status, body = await self.panel.dispatch("POST", "import")
        self.assertEqual(status, 400)
        self.assertIn("私聊目标", body["error"])
        self.service.store.import_records.assert_not_called()
