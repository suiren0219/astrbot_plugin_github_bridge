"""仅本机的隔离演示服务；不会请求 GitHub 或发送 QQ 消息。"""

import argparse
import asyncio
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_bridge.models import Event, Observation
from github_bridge.service import BridgeService
from github_bridge.settings import SettingsStore
from github_bridge.storage import Store
from tools.preview_server import PreviewServer


class DemoGitHub:
    def __init__(self, settings):
        self.settings = settings
        self.rate_limit = {
            "remaining": 4936,
            "limit": 5000,
            "reset": time.time() + 2800,
        }

    async def close(self):
        pass

    async def test_connection(self):
        return {
            "login": self.settings.github_owner,
            "repository_count": len(self.repos()),
        }

    async def list_repositories(self):
        # 留出可观察的后台运行时间，验证排队后的状态轮询。
        await asyncio.sleep(3)
        return self.repos()

    def repos(self):
        definitions = [
            ("gdust-v2", "让校园生活，轻一点。课表、教务与学习助手。", 42, 6),
            (
                "astrbot_plugin_schedule_lite",
                "把课表放进聊天，让每一次出发都有准备。",
                27,
                3,
            ),
            (
                "astrbot_plugin_ai_rights",
                "为 AstrBot 提供更自然的消息交互体验。",
                18,
                2,
            ),
            (
                "astrbot_plugin_github_bridge",
                "QQ 与 GitHub 之间，一座安静工作的桥。",
                5,
                1,
            ),
            ("GKDX", "轻量、可控的 Android 自动跳过工具。", 38, 4),
            ("astrbot_plugin_schedule_lite_web", "课表 Lite 的网页伴侣。", 9, 1),
        ]
        return [
            {
                "id": i + 1,
                "full_name": f"{self.settings.github_owner}/{name}",
                "description": desc,
                "private": False,
                "archived": False,
                "default_branch": "main",
                "stargazers_count": stars,
                "forks_count": forks,
                "open_issues_count": 2 if i == 0 else 0,
                "html_url": f"https://github.com/{self.settings.github_owner}/{name}",
            }
            for i, (name, desc, stars, forks) in enumerate(definitions)
        ]

    async def observe(self, repo, previous):
        return Observation({"demo": True})

    async def create_issue(self, repo, title, body):
        raise ValueError("演示服务不会向 GitHub 提交内容")


class DemoService(BridgeService):
    def status(self):
        return {**super().status(), "preview": True}


async def main(port: int, event_delay: float = 0):
    with tempfile.TemporaryDirectory(prefix="github-bridge-preview-") as temp:
        root = Path(temp)
        settings = SettingsStore(
            root,
            {
                "github_token": "demo-placeholder-not-a-token",
                "target_umo": "demo-bot:FriendMessage:123456",
                "enabled": True,
            },
        )
        store = Store(root / "bridge.sqlite3")
        cfg = settings.value

        async def simulated_send(target, text):
            return None

        service = DemoService(settings, store, simulated_send, DemoGitHub)
        repos = service.client.repos()
        store.discover(cfg.identity, repos)
        samples = [
            (
                0,
                "commit",
                "fix: 修复学习通课程导入",
                "更新了课程识别与重试逻辑。",
                "commit/demo",
            ),
            (
                1,
                "release",
                "课表 Lite v1.10.7",
                "日程同步与卡片展示体验更新。",
                "releases",
            ),
            (
                0,
                "issue-event",
                "#8 后台退出问题已关闭",
                "Issue 状态：open → closed",
                "issues/8",
            ),
            (4, "metadata", "仓库获得了新的关注", "Star：37 → 38", "stargazers"),
        ]
        for n, (index, kind, title, body, suffix) in enumerate(samples):
            repo = repos[index]
            event = Event(
                f"demo:{n}",
                repo["full_name"],
                kind,
                title,
                body,
                f"{repo['html_url']}/{suffix}",
                datetime.now(timezone.utc).isoformat(),
            )
            store.record(
                cfg.identity, cfg.target_umo, repo, Observation({"demo": True}, [event])
            )
        for index, event in enumerate(store.queue(cfg.identity, cfg.target_umo, 10)):
            store.mark_sending(event["id"])
            if index == 3:
                store.mark_failed(event["id"], "演示：QQ 平台暂时离线，等待重试")
            else:
                store.mark_sent(event["id"])
        service.last_check = time.time() - 38
        service.next_check = time.time() + 82
        panel = PreviewServer(
            service,
            Path(__file__).resolve().parents[1] / "views" / "dashboard",
            event_delay=event_delay,
        )
        await panel.start("127.0.0.1", port)
        print(f"本地界面预览 http://127.0.0.1:{port} · 不连接 GitHub / QQ", flush=True)
        try:
            # 演示仅按界面的“立即检查”请求刷新，不产生后台外部操作。
            while True:
                await service._wake.wait()
                service._wake.clear()
                await service.check()
                service.next_check = time.time() + settings.value.poll_interval
        finally:
            await panel.close()
            await service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--event-delay",
        type=float,
        default=0,
        help="全部通知响应延迟秒数，用于测试筛选竞态",
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(main(arguments.port, max(0, arguments.event_delay)))
    except KeyboardInterrupt:
        pass
