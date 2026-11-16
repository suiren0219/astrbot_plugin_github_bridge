from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Event
from .settings import REPO_PATTERN, Settings


@dataclass
class Migration:
    seen: list[str] = field(default_factory=list)
    failed: list[Event] = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def parse_legacy(sent: Any, failed: Any, settings: Settings) -> Migration:
    result = Migration()
    if sent is not None and not isinstance(sent, dict):
        raise ValueError("sent_notifications.json 应为 仓库 → SHA → 目标组 的 JSON 对象")
    if failed is not None and not isinstance(failed, list):
        raise ValueError("failed_notifications.json 应为 JSON 数组")
    if sent is None and failed is None:
        raise ValueError("请提供已发送或失败通知 JSON")
    if len(sent or {}) > 20000 or len(failed or []) > 20000:
        raise ValueError("单次最多导入 20000 条记录")

    def allowed(repo: str) -> bool:
        return bool(REPO_PATTERN.fullmatch(repo)) and repo.split("/", 1)[0].casefold() == settings.identity and not settings.excludes(repo)

    for repo_key, commits in (sent or {}).items():
        # branch 可以包含 /，只有最前两段是 owner/repo。
        repo = "/".join(repo_key.split("/")[:2])
        if not allowed(repo) or not isinstance(commits, dict):
            result.skipped += len(commits) if isinstance(commits, dict) else 1
            continue
        for sha in commits:
            if re.fullmatch(r"[a-fA-F0-9]{7,64}", sha):
                result.seen.append(f"{repo.casefold()}@{sha.lower()}")
            else:
                result.skipped += 1
    target_id = settings.target_umo.rsplit(":", 1)[-1]
    ignored_targets = 0
    for entry in failed or []:
        if not isinstance(entry, dict):
            result.skipped += 1
            continue
        repo_info = entry.get("repo_info") or {}
        if not isinstance(repo_info, dict):
            result.skipped += 1
            continue
        owner = repo_info.get("owner") or {}
        repo = repo_info.get("full_name") or f"{owner.get('login', '') if isinstance(owner, dict) else ''}/{repo_info.get('name', '')}"
        if not isinstance(repo, str) or not allowed(repo):
            result.skipped += 1
            continue
        targets = entry.get("targets", [])
        if not isinstance(targets, list) or not {str(t).strip() for t in targets}.intersection({target_id, settings.target_umo}):
            result.skipped += 1
            ignored_targets += 1
            continue
        commits = entry.get("new_commits", [])
        if not isinstance(commits, list) or not commits:
            result.skipped += 1
            continue
        for commit in commits:
            if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str) or not re.fullmatch(r"[a-fA-F0-9]{7,64}", commit["sha"]):
                result.skipped += 1
                continue
            sha = commit["sha"].lower()
            alias = f"{repo.casefold()}@{sha}"
            message = str(commit.get("message") or "历史提交")[:1800]
            # URL 从已校验的 repo/sha 重建，忽略旧文件中的任意链接与群目标。
            result.failed.append(Event(
                key=f"legacy-retry:{alias}", repo=repo, kind="legacy", title=message.splitlines()[0][:200],
                body=f"旧监控失败通知重试\n{message}\n提交：{sha[:12]}",
                url=f"https://github.com/{repo}/commit/{sha}", occurred_at=str(commit.get("date") or entry.get("created_at") or "")[:100],
                legacy_keys=(alias,),
            ))
    if ignored_targets:
        result.warnings.append(f"跳过 {ignored_targets} 条不属于当前管理员私聊的失败通知；群聊目标不会导入。")
    if result.failed:
        result.warnings.append("旧插件可能同时记录已发送与部分目标失败，因此管理员私聊失败项优先保留；启用监控后按每轮上限重试。")
    result.warnings.append("支持 Shell 旧监控的已发送/失败文件；旧 Issue 快照无需导入，新插件首次检查将静音建立基线。")
    return result
