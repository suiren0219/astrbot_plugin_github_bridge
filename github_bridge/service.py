from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from . import __version__
from .settings import SettingsStore
from .storage import Store

Sender = Callable[[str, str], Awaitable[None]]


class BridgeService:
    def __init__(
        self,
        settings: SettingsStore,
        store: Store,
        sender: Sender,
        client_factory=None,
        summarizer=None,
    ):
        self.settings = settings
        self.store = store
        self.sender = sender
        self.summarizer = summarizer
        self.summary_warning = ""
        self._summary_cooldown = 0.0
        if client_factory is None:
            from .github import GitHubClient

            client_factory = GitHubClient
        self.client_factory = client_factory
        self.client = self.client_factory(settings.value)
        self.running = False
        self.last_error = ""
        self.last_check = store.meta("last_check")
        self.next_check: float | None = None
        self.cooldown_until = 0.0
        self._holder = secrets.token_hex(16)
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._recovered = False
        self._check_lock = asyncio.Lock()

    def safe_error(self, error: Exception) -> str:
        value = str(error) or type(error).__name__
        token = self.settings.value.github_token
        if token:
            value = value.replace(token, "[已隐藏]")
        value = re.sub(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+", "[已隐藏]", value)
        value = re.sub(
            r"(?i)(authorization|bearer|token|password)[=: ]+[^\s,;]+",
            r"\1=[已隐藏]",
            value,
        )
        return value[:400]

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="github-bridge-monitor")

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        async with self._check_lock:
            await self.client.close()
        self.store.release(self._holder)
        self.store.close()

    async def _loop(self) -> None:
        while not self._closed:
            cfg = self.settings.value
            self._wake.clear()
            delay = cfg.poll_interval
            if cfg.enabled:
                try:
                    await self.check()
                except Exception as exc:
                    self.last_error = self.safe_error(exc)
                    logger.warning(f"GitHub Bridge 检查失败：{self.last_error}")
                self.next_check = time.time() + delay
            else:
                self.next_check = None
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def request_check(self) -> None:
        if not self.settings.value.enabled:
            raise ValueError("请先启用监控；连通测试可在暂停时使用")
        if self.running:
            raise ValueError("本轮检查仍在进行中")
        if time.time() < self.cooldown_until:
            raise ValueError("GitHub 正在限流，恢复时间前不会重复请求")
        self._wake.set()

    async def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if self._check_lock.locked():
            raise ValueError("检查正在进行，请稍后保存配置")
        async with self._check_lock:
            previous = self.settings.value
            value = self.settings.update(patch)
            if value != previous:
                await self.client.close()
                self.client = self.client_factory(value)
                self.last_error = ""
                if (
                    value.ai_summary_enabled != previous.ai_summary_enabled
                    or value.ai_summary_provider_id != previous.ai_summary_provider_id
                ):
                    self.summary_warning = ""
                    self._summary_cooldown = 0.0
                self._wake.set()
        return value.public()

    async def test_connection(self) -> dict[str, Any]:
        if not self.settings.value.github_owner:
            raise ValueError("请先保存自己的 GitHub 用户名")
        if not self.settings.value.github_token:
            raise ValueError("请先保存 GitHub Token")
        if time.time() < self.cooldown_until:
            raise ValueError("GitHub 限流中，请等待恢复后测试")
        client = self.client_factory(self.settings.value)
        try:
            return await client.test_connection()
        except Exception as exc:
            self._rate_error(exc)
            raise ValueError(self.safe_error(exc)) from None
        finally:
            await client.close()

    def _rate_error(self, error: Exception) -> bool:
        retry_at = getattr(error, "retry_at", None)
        if retry_at:
            self.cooldown_until = max(self.cooldown_until, float(retry_at))
            return True
        return False

    async def check(self) -> dict[str, int]:
        if self._check_lock.locked():
            return {"queued": 0, "sent": 0}
        async with self._check_lock:
            cfg = self.settings.value
            if not cfg.enabled:
                return {"queued": 0, "sent": 0}
            lease_ttl = max(180, cfg.request_timeout * 3)
            if not self.store.acquire(self._holder, lease_ttl):
                return {"queued": 0, "sent": 0}
            self.running = True
            queued = sent = 0
            failures = []
            heartbeat = asyncio.create_task(self._heartbeat(lease_ttl))
            try:
                if not self._recovered:
                    self.store.recover_interrupted()
                    self._recovered = True
                try:
                    if time.time() < self.cooldown_until:
                        raise RuntimeError(
                            "GitHub 限流退避中，已暂停抓取；已落盘通知仍可投递"
                        )
                    repos = await self.client.list_repositories()
                    repos = [
                        repo for repo in repos if not cfg.excludes(repo["full_name"])
                    ]
                    self.store.discover(cfg.identity, repos)
                    for repo in repos:
                        try:
                            previous = self.store.snapshot(cfg.identity, repo["id"])
                            observation = await self.client.observe(repo, previous)
                            queued += self.store.record(
                                cfg.identity, cfg.target_umo, repo, observation
                            )
                        except Exception as exc:
                            error = self.safe_error(exc)
                            failures.append(f"{repo['full_name']}: {error}")
                            self.store.record_error(cfg.identity, repo["id"], error)
                            if self._rate_error(exc):
                                break
                except Exception as exc:
                    failures.append(self.safe_error(exc))
                    self._rate_error(exc)
                # GitHub 暂时不可用也继续投递已落盘通知。
                for event in self.store.queue(
                    cfg.identity, cfg.target_umo, cfg.max_push_per_cycle
                ):
                    summary = await self.summarize_event(event)
                    self.store.mark_sending(event["id"])
                    try:
                        await asyncio.wait_for(
                            self.sender(
                                cfg.target_umo, self.format_event(event, summary)
                            ),
                            timeout=30,
                        )
                        self.store.mark_sent(event["id"])
                        sent += 1
                    except Exception as exc:
                        error = self.safe_error(exc)
                        self.store.mark_failed(event["id"], error)
                        failures.append(f"私聊发送失败：{error}")
                self.last_check = time.time()
                self.store.set_meta("last_check", self.last_check)
                self.last_error = "；".join(failures[:3])
                logger.info(
                    f"GitHub Bridge CHECK queued={queued} sent={sent} "
                    f"failures={len(failures)}"
                )
                return {"queued": queued, "sent": sent}
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                finally:
                    self.store.release(self._holder)
                    self.running = False

    async def _heartbeat(self, ttl: int) -> None:
        while True:
            await asyncio.sleep(30)
            if not self.store.acquire(self._holder, ttl):
                raise RuntimeError("轮询锁已丢失")

    async def summarize_event(self, event: dict[str, Any]) -> str:
        cfg = self.settings.value
        if not cfg.ai_summary_enabled or self.summarizer is None:
            return ""
        if not cfg.ai_summary_private and not self.store.is_public_event(
            event, cfg.identity
        ):
            return ""
        cached = self.store.summary(event["id"])
        if cached is not None:
            return cached["summary"]
        if time.time() < self._summary_cooldown:
            return ""
        try:
            from .summary import clean_summary

            text = await asyncio.wait_for(
                self.summarizer(event, cfg), timeout=cfg.ai_summary_timeout
            )
            if not isinstance(text, str):
                raise ValueError("模型未返回文本摘要")
            text = clean_summary(text, cfg.github_token)
        except Exception:
            # 模型故障不影响原始通知；不回显供应商异常，防止其中包含请求和凭据。
            self.summary_warning = (
                "AI 总结暂不可用，已回退为原始通知；稍后会为新通知恢复尝试。"
            )
            self._summary_cooldown = time.time() + 60
            self.store.save_summary(event["id"], "", "failed")
            return ""
        self.store.save_summary(event["id"], text, "ready")
        self.summary_warning = ""
        return text

    @staticmethod
    def format_event(event: dict[str, Any], summary: str = "") -> str:
        kinds = {
            "commit": "代码提交",
            "release": "新 Release",
            "issue": "Issue 动态",
            "issue-event": "Issue 动态",
            "comment": "Issue 评论",
            "metadata": "仓库变化",
            "legacy": "历史通知重试",
        }
        label = kinds.get(event["kind"], event["kind"])
        title = event["title"]
        body = event["body"]
        if event["kind"] == "commit":
            subject, _, details = body.partition("\n")
            subject = subject.rstrip("\r")
            # 兼容已落盘消息：采集器把提交首行同时放进 title 和 body。
            # title 在采集时最多保留 300 字符，正文中的首行可能更长。
            truncated_subject = (
                len(title) == 300
                and title.endswith("…")
                and len(subject) > 300
                and subject.startswith(title[:-1])
            )
            if subject == title.rstrip("\r") or truncated_subject:
                body = details.lstrip("\r\n")
        lines = [f"[GitHub · {label}]", event["repo"], title[:200]]
        if body.strip():
            lines.append(body[:1400])
        if summary:
            lines.append(f"\nAI 总结：{summary}")
        if event["url"]:
            lines.append(event["url"])
        return "\n".join(lines).strip()

    def status(self) -> dict[str, Any]:
        cfg = self.settings.value
        return {
            "version": __version__,
            "enabled": cfg.enabled,
            "ready": bool(cfg.github_owner and cfg.github_token and cfg.target_umo),
            "running": self.running,
            "github_owner": cfg.github_owner,
            "target_umo": cfg.target_umo,
            "poll_interval": cfg.poll_interval,
            "last_check": self.last_check,
            "next_check": self.next_check,
            "last_error": self.last_error,
            "configuration_warning": self.settings.configuration_warning,
            "ai_summary_enabled": cfg.ai_summary_enabled,
            "summary_warning": self.summary_warning,
            "rate_limit": getattr(self.client, "rate_limit", {}),
            "cooldown_until": self.cooldown_until,
            "counts": self.store.counts(cfg.identity),
            "recent_events": self.store.events(cfg.identity, limit=5),
            "preview": False,
        }

    async def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]:
        cfg = self.settings.value
        if "/" not in repo:
            repo = f"{cfg.github_owner}/{repo}"
        if not cfg.allows_issue(repo):
            raise ValueError("该仓库不在 issue 允许列表中")
        if not 1 <= len(title.strip()) <= 256 or not 1 <= len(body.strip()) <= 60000:
            raise ValueError("标题须为 1–256 字，正文须为 1–60000 字")
        if not cfg.github_token:
            raise ValueError("请先配置 GitHub Token")
        client = self.client_factory(cfg)
        try:
            return await client.create_issue(repo, title.strip(), body.strip())
        except Exception as exc:
            self._rate_error(exc)
            raise ValueError(self.safe_error(exc)) from None
        finally:
            await client.close()
