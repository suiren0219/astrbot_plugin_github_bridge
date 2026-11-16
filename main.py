import asyncio
import re
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .github_bridge.feedback import Drafts, group_context
from .github_bridge.panel import PanelAPI
from .github_bridge.service import BridgeService
from .github_bridge.settings import SettingsStore
from .github_bridge.storage import Store


PLUGIN_NAME = "astrbot_plugin_github_bridge"

HELP = """GitHub Bridge · 管理员私聊
/gh status — 监控、队列与运行状态
/gh panel — 查看管理页入口
/gh check — 立即检查（需已启用）
/gh pause 或 /gh resume — 暂停 / 恢复
/gh retry — 将失败通知重新排队
/gh config — 查看脱敏配置
/gh logs — 查看最近失败记录
/gh baseline 仓库 确认 — 暂停后重建单仓库基线（跳过未抓取历史）
/gh bug 仓库 | 标题 | 正文 — 创建 issue 草稿
/gh fromgroup 群号 仓库 | 标题 | 补充说明 — 将最近 20 条群文本放入草稿
/gh confirm 草稿码 — 确认发布到 GitHub
/gh cancel 草稿码 — 取消草稿
管理面板位于 AstrBot Dashboard → 插件 → GitHub Bridge → 可视化视图，使用 Dashboard 登录，无独立端口或口令。仓库可以用 gdust-v2 或完整 owner/repo；只有允许列表内的本人仓库可提交。草稿 15 分钟后失效。"""


class GitHubBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.boot = config or {}
        self.service: BridgeService | None = None
        self.panel: PanelAPI | None = None
        self.drafts = Drafts()
        self.started = time.time()
        # 4.29.4+ 的插件页面由 Dashboard 自动扫描 views/；接口沿用它已有的登录态。
        for method, endpoints in (("GET", PanelAPI.GET_ROUTES), ("POST", PanelAPI.POST_ROUTES)):
            for endpoint in endpoints:
                handler = self._make_web_handler(method, endpoint)
                context.register_web_api(f"/{PLUGIN_NAME}/{endpoint}", handler, [method], "GitHub Bridge 管理页接口")

    def _make_web_handler(self, method: str, path: str):
        async def handler(**kwargs):
            if self.service is None or self.panel is None:
                from astrbot.api.web import error_response
                return error_response("GitHub Bridge 尚未初始化", status_code=503)
            return await self.panel.dispatch(method, path.lstrip("/"))
        return handler

    async def initialize(self):
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        settings = SettingsStore(data_dir)
        store = Store(data_dir / "bridge.sqlite3")
        try:
            self.service = BridgeService(settings, store, self._send_private)
            self.panel = PanelAPI(self.service)
            await self.service.start()
            logger.info("GitHub Bridge 已启动；管理页由 AstrBot Dashboard 插件视图承载")
        except Exception:
            if self.service:
                await self.service.close()
            else:
                store.close()
            self.service = None
            self.panel = None
            raise

    async def terminate(self):
        if self.service:
            await self.service.close()
            self.service = None
            self.panel = None

    async def _send_private(self, target: str, text: str):
        if not re.fullmatch(r"[^:\s]+:FriendMessage:[^:\s]+", target):
            raise ValueError("GitHub Bridge 仅允许发送私聊")
        accepted = await self.context.send_message(target, MessageChain().message(text))
        if not accepted:
            raise RuntimeError("私聊平台未找到或尚未上线，请检查目标中的平台实例 ID")

    @filter.command("gh", alias={"github桥"})
    async def gh(self, event: AstrMessageEvent):
        event.stop_event()
        # 业务层固定权限，不能被面板命令权限覆盖；群里保持静默。
        if not event.is_admin() or not event.is_private_chat():
            return
        if self.service is None:
            yield event.plain_result("GitHub Bridge 尚未初始化，请检查插件启动日志。")
            return
        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else "help"
        args = raw.split(maxsplit=1)
        command = args[0].casefold()
        rest = args[1].strip() if len(args) > 1 else ""
        sender = event.unified_msg_origin
        try:
            if command in {"help", "帮助"}:
                reply = HELP
            elif command == "status":
                status = self.service.status()
                counts = status["counts"]
                reply = (
                    f"GitHub Bridge v{status['version']}\n"
                    f"状态：{'检查中' if status['running'] else '已启用' if status['enabled'] else '已暂停'}\n"
                    f"仓库：{counts['repositories']} · 待推：{counts['pending']} · 成功：{counts['sent']} · 失败：{counts['failed']}\n"
                    f"运行：{int((time.time() - self.started) / 60)} 分钟 · 周期：{status['poll_interval']} 秒\n"
                    f"目标：{status['target_umo']}\n"
                    f"最近错误：{status['last_error'] or '无'}"
                )
            elif command == "panel":
                reply = "管理面板：AstrBot Dashboard → 插件管理 → GitHub Bridge → 可视化视图 dashboard。沿用 Dashboard 登录，无需独立端口与口令。"
            elif command == "check":
                self.service.request_check()
                reply = "已请求立即检查。首次检查会静音建立基线，后续通知只发到配置的管理员私聊。"
            elif command in {"pause", "resume"}:
                await self.service.update_settings({"enabled": command == "resume"})
                reply = "监控已恢复。" if command == "resume" else "监控已暂停，待发通知将保留。"
            elif command == "retry":
                cfg = self.service.settings.value
                count = self.service.store.retry(cfg.identity, cfg.target_umo)
                reply = f"已将 {count} 条失败通知重新排队，下轮启用中的检查会按上限发送。"
            elif command == "config":
                cfg = self.service.settings.value
                reply = f"GitHub：{cfg.github_owner}\nToken：{'已配置' if cfg.github_token else '未配置'}\n私聊：{cfg.target_umo}\n周期：{cfg.poll_interval}s · 每轮上限：{cfg.max_push_per_cycle}\n允许反馈：{cfg.issue_repositories}"
            elif command == "baseline":
                cfg = self.service.settings.value
                if cfg.enabled or self.service.running:
                    raise ValueError("请先 /gh pause 并等待本轮结束，再重建基线")
                baseline_args = rest.split()
                if len(baseline_args) != 2 or baseline_args[1] != "确认":
                    raise ValueError("此操作会跳过该仓库尚未抓取的历史，保留已入队通知。确认后发送：/gh baseline 仓库 确认")
                repo = baseline_args[0]
                if "/" not in repo:
                    repo = f"{cfg.github_owner}/{repo}"
                if not self.service.store.reset_snapshot(cfg.identity, repo):
                    raise ValueError("当前监控记录中未找到这个仓库")
                reply = "该仓库下次检查将静音建立新基线；已有通知队列保持不变。使用 /gh resume 恢复。"
            elif command == "logs":
                events = self.service.store.events(self.service.settings.value.identity, "failed", 5)
                reply = "最近失败记录\n" + ("\n".join(f"#{e['id']} {e['repo']}：{e['last_error']}" for e in events) or "暂无失败记录")
                if self.service.last_error:
                    reply += f"\n本轮检查：{self.service.last_error}"
            elif command in {"bug", "fromgroup"}:
                fields = [field.strip() for field in rest.split("|", 2)]
                if len(fields) != 3:
                    raise ValueError("格式：/gh bug 仓库 | 标题 | 正文；或 /gh fromgroup 群号 仓库 | 标题 | 补充说明")
                repo, title, body = fields
                if command == "fromgroup":
                    source = repo.split(maxsplit=1)
                    if len(source) != 2 or not source[0].isdigit():
                        raise ValueError("请填写 群号 仓库，例如 /gh fromgroup 123456 gdust-v2 | 标题 | 说明")
                    group_id, repo = source
                    body += "\n\n### 群聊文本节选（发布前请核对）\n\n" + await self._group_history(event, int(group_id))
                if "/" not in repo:
                    repo = f"{self.service.settings.value.github_owner}/{repo}"
                if not self.service.settings.value.allows_issue(repo):
                    raise ValueError("该仓库不在本人 issue 允许列表中")
                draft = self.drafts.create(sender, repo, title, body)
                reply = f"Issue 草稿 · {draft.repo}\n标题：{draft.title}\n\n{draft.body}\n\n将公开或提交到目标仓库，请核对上述完整内容。\n确认：/gh confirm {draft.code}\n取消：/gh cancel {draft.code}\n15 分钟后失效；尚未向 GitHub 发送。"
            elif command == "confirm":
                draft = self.drafts.pop(rest, sender)
                try:
                    issue = await self.service.create_issue(draft.repo, draft.title, draft.body)
                    reply = f"Issue 已创建：{issue['html_url']}"
                except Exception:
                    # 网络超时有可能 GitHub 已受理，不自动重放同一写请求。
                    raise ValueError("提交未能确认成功。请先检查仓库是否已创建同标题 issue，再重新生成草稿；本草稿已失效。") from None
            elif command == "cancel":
                self.drafts.pop(rest, sender)
                reply = "草稿已取消。"
            else:
                reply = HELP
            yield event.plain_result(reply)
        except (ValueError, RuntimeError) as exc:
            yield event.plain_result(self.service.safe_error(exc))
        finally:
            event.stop_event()

    async def _group_history(self, event: AstrMessageEvent, group_id: int) -> str:
        bot = getattr(event, "bot", None)
        if event.get_platform_name() != "aiocqhttp" or bot is None or not hasattr(bot, "call_action"):
            raise ValueError("当前适配器不支持读取群历史；请手动粘贴相关文本到 bug 草稿")
        try:
            payload = await asyncio.wait_for(bot.call_action("get_group_msg_history", group_id=group_id, count=20, self_id=event.get_self_id()), timeout=20)
            if not isinstance(payload, dict):
                raise ValueError("群历史返回格式不正确")
            return group_context(payload)
        except Exception:
            raise ValueError("无法读取群历史，请确认机器人在群内且 OneBot/NapCat 支持 get_group_msg_history；也可手动粘贴相关文本") from None
