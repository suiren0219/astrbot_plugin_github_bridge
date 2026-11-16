import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from github_bridge.settings import Settings, SettingsStore

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.web",
            "astrbot.api.event",
            "astrbot.api.star",
        )
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = Mock()

    class Star:
        def __init__(self, context, config=None):
            self.context = context

    class MessageChain:
        def message(self, text):
            self.text = text
            return self

    modules["astrbot.api.web"].error_response = lambda message, status_code=400: {
        "error": message,
        "status_code": status_code,
    }
    modules["astrbot.api.web"].json_response = lambda obj: obj
    modules["astrbot.api.web"].request = types.SimpleNamespace(
        username="tester", query=types.SimpleNamespace(get=lambda *a, **kw: None)
    )
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=lambda *a, **kw: lambda fn: fn
    )
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].StarTools = Mock()
    package = types.ModuleType("bridge_plugin_test")
    package.__path__ = [str(ROOT)]
    modules[package.__name__] = package
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "bridge_plugin_test.main", ROOT / "main.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


plugin_module = load_plugin()


class Event:
    def __init__(self, text, admin=True, private=True, sender="qq:FriendMessage:123"):
        self.message_str = text
        self.unified_msg_origin = sender
        self.admin, self.private = admin, private
        self.stopped = False
        self.bot = types.SimpleNamespace(
            call_action=AsyncMock(
                return_value={
                    "messages": [
                        {"message": [{"type": "text", "data": {"text": "导入失败"}}]}
                    ]
                }
            )
        )

    def is_admin(self):
        return self.admin

    def is_private_chat(self):
        return self.private

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def get_platform_name(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "bot123"


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registered = []
        self.context = types.SimpleNamespace(
            send_message=AsyncMock(return_value=True),
            register_web_api=lambda route, handler, methods, desc: (
                self.registered.append((route, methods))
            ),
        )
        self.plugin = plugin_module.GitHubBridge(self.context, {})
        self.service = Mock()
        self.service.settings.value = Settings(github_token="unit-test-token")
        self.service.safe_error = str
        self.service.create_issue = AsyncMock(
            return_value={"html_url": "https://github.com/suiren0219/gdust-v2/issues/1"}
        )
        self.service.update_settings = AsyncMock()
        self.plugin.service = self.service

    async def command(self, event):
        return [reply async for reply in self.plugin.gh(event)]

    async def test_initialize_applies_boot_pause_and_terminate_closes_service(self):
        for auto_start in (False, True):
            with (
                self.subTest(auto_start=auto_start),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                SettingsStore(
                    root,
                    {
                        "github_token": "test-token",
                        "target_umo": "qq:FriendMessage:1",
                        "enabled": True,
                    },
                )
                service = Mock(start=AsyncMock(), close=AsyncMock())
                captured = []

                def factory(settings, store, sender):
                    captured.append((settings, store))
                    return service

                plugin = plugin_module.GitHubBridge(
                    self.context, {"monitor_enabled": auto_start}
                )
                with (
                    patch.object(
                        plugin_module.StarTools, "get_data_dir", return_value=root
                    ),
                    patch.object(plugin_module, "BridgeService", side_effect=factory),
                    patch.object(plugin_module, "PanelAPI"),
                ):
                    await plugin.initialize()
                    self.assertEqual(captured[0][0].value.enabled, auto_start)
                    service.start.assert_awaited_once()
                    await plugin.terminate()
                    service.close.assert_awaited_once()
                    self.assertIsNone(plugin.service)
                    self.assertIsNone(plugin.panel)
                    captured[0][1].close()

    async def test_ordinary_user_and_group_are_silent(self):
        for event in (
            Event("gh panel", admin=False),
            Event("gh bug gdust-v2 | 标题 | 内容", private=False),
        ):
            self.assertEqual(await self.command(event), [])
            self.assertTrue(event.stopped)
        self.service.create_issue.assert_not_awaited()

    async def test_bug_requires_confirmation_and_draft_cannot_be_reused(self):
        reply = await self.command(Event("gh bug gdust-v2 | 标题 | 正文\n第二行"))
        self.assertIn("尚未向 GitHub 发送", reply[0])
        self.service.create_issue.assert_not_awaited()
        code = next(iter(self.plugin.drafts.items))
        wrong = await self.command(
            Event(f"gh confirm {code}", sender="qq:FriendMessage:456")
        )
        self.assertIn("不属于", wrong[0])
        self.service.create_issue.assert_not_awaited()
        result = await self.command(Event(f"gh confirm {code}"))
        self.assertIn("Issue 已创建", result[0])
        self.service.create_issue.assert_awaited_once_with(
            "suiren0219/gdust-v2", "标题", "正文\n第二行"
        )
        await self.command(Event(f"gh confirm {code}"))
        self.assertEqual(self.service.create_issue.await_count, 1)

    async def test_disallowed_repository_never_creates_draft(self):
        result = await self.command(Event("gh bug other/repo | 标题 | 内容"))
        self.assertIn("允许列表", result[0])
        self.assertEqual(self.plugin.drafts.items, {})

    async def test_context_send_false_is_failure_and_group_send_is_blocked(self):
        self.context.send_message.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未找到"):
            await self.plugin._send_private("QQ:FriendMessage:123", "hello")
        with self.assertRaises(ValueError):
            await self.plugin._send_private("QQ:GroupMessage:123", "hello")
        self.assertEqual(self.context.send_message.await_count, 1)

    async def test_dashboard_routes_are_registered_with_plugin_prefix(self):
        routes = [route for route, _ in self.registered]
        self.assertIn("/astrbot_plugin_github_bridge/status", routes)
        self.assertIn("/astrbot_plugin_github_bridge/settings", routes)
        self.assertTrue(
            all(route.startswith("/astrbot_plugin_github_bridge/") for route in routes)
        )
        replies = await self.command(Event("gh panel"))
        self.assertIn("Dashboard", replies[0])
        self.assertNotIn("新登录口令", replies[0])

    async def test_group_history_uses_exact_backend_action_and_does_not_publish(self):
        event = Event("gh fromgroup 98765 gdust-v2 | 导入错误 | 复现信息")
        result = await self.command(event)
        event.bot.call_action.assert_awaited_once_with(
            "get_group_msg_history", group_id=98765, count=20, self_id="bot123"
        )
        self.assertIn("导入失败", result[0])
        self.service.create_issue.assert_not_awaited()

    async def test_config_never_exposes_saved_token(self):
        result = await self.command(Event("gh config"))
        self.assertIn("已配置", result[0])
        self.assertNotIn("unit-test-token", result[0])

    async def test_failed_issue_write_does_not_automatically_retry(self):
        await self.command(Event("gh bug gdust-v2 | 标题 | 正文"))
        code = next(iter(self.plugin.drafts.items))
        self.service.create_issue.side_effect = TimeoutError()
        result = await self.command(Event(f"gh confirm {code}"))
        self.assertIn("先检查仓库", result[0])
        self.assertNotIn(code, self.plugin.drafts.items)
        self.assertEqual(self.service.create_issue.await_count, 1)
