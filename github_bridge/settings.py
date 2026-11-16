from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .settings_migration import SETTINGS_SCHEMA_VERSION, clear_legacy_presets

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass
class Settings:
    enabled: bool = False
    github_owner: str = ""
    github_token: str = ""
    target_umo: str = ""
    poll_interval: int = 120
    max_push_per_cycle: int = 5
    monitor_commits: bool = True
    monitor_releases: bool = True
    monitor_issues: bool = True
    monitor_metadata: bool = True
    issue_repositories: str = ""
    excluded_repositories: str = ""
    request_timeout: int = 25
    max_pages: int = 20

    def validate(self) -> None:
        if not isinstance(self.github_owner, str) or (
            self.github_owner
            and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", self.github_owner)
        ):
            raise ValueError("GitHub 用户名格式不正确")
        if not isinstance(self.target_umo, str) or (
            self.target_umo
            and not re.fullmatch(r"[^:\s]+:FriendMessage:[^:\s]+", self.target_umo)
        ):
            raise ValueError(
                "推送目标必须为空或为 平台ID:FriendMessage:用户ID，只支持私聊"
            )
        for key, lo, hi in (
            ("poll_interval", 60, 86400),
            ("max_push_per_cycle", 1, 20),
            ("request_timeout", 5, 120),
            ("max_pages", 1, 100),
        ):
            value = getattr(self, key)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{key} 必须为 {lo}–{hi} 之间的整数")
        for key in (
            "enabled",
            "monitor_commits",
            "monitor_releases",
            "monitor_issues",
            "monitor_metadata",
        ):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} 必须为布尔值")
        for key in ("github_token", "issue_repositories", "excluded_repositories"):
            value = getattr(self, key)
            if not isinstance(value, str) or len(value) > 4096:
                raise ValueError(f"{key} 格式不正确或过长")
        if self.enabled and not self.github_owner:
            raise ValueError("启用监控前请填写自己的 GitHub 用户名")
        if self.enabled and not self.github_token.strip():
            raise ValueError("启用监控前请保存 GitHub Token")
        if self.enabled and not self.target_umo:
            raise ValueError("启用监控前请先填写管理员私聊目标")

    @property
    def identity(self) -> str:
        return self.github_owner.casefold()

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("github_token")
        data["token_configured"] = bool(self.github_token)
        return data

    def allows_issue(self, full_name: str) -> bool:
        if not REPO_PATTERN.fullmatch(full_name):
            return False
        owner, name = full_name.split("/", 1)
        if owner.casefold() != self.identity:
            return False
        for pattern in self.issue_repositories.split(","):
            pattern = pattern.strip().casefold()
            candidate = full_name.casefold() if "/" in pattern else name.casefold()
            if pattern and (
                candidate == pattern
                or pattern.endswith("*")
                and candidate.startswith(pattern[:-1])
            ):
                return True
        return False

    def excludes(self, full_name: str) -> bool:
        exclusions = {
            x.strip().casefold()
            for x in self.excluded_repositories.split(",")
            if x.strip()
        }
        return (
            full_name.casefold() in exclusions
            or full_name.split("/", 1)[-1].casefold() in exclusions
        )


def secure_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


class SettingsStore:
    def __init__(self, data_dir: Path, initial: dict[str, Any] | None = None):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(data_dir, 0o700)
        self.path = data_dir / "settings.json"
        valid_keys = {field.name for field in fields(Settings)}
        data = {
            key: value for key, value in (initial or {}).items() if key in valid_keys
        }
        original = None
        changed = []
        if self.path.exists():
            original = self.path.read_text(encoding="utf-8")
            saved = json.loads(original)
            if not isinstance(saved, dict):
                raise ValueError("settings.json 必须是 JSON 对象")
            cleaned, changed = clear_legacy_presets(saved)
            data.update(cleaned)
        notices = data.get("_cleared_legacy_presets", [])
        self.cleared_legacy_presets = (
            [
                key
                for key in notices
                if isinstance(key, str)
                and key in {"github_owner", "issue_repositories", "target_umo"}
            ]
            if isinstance(notices, list)
            else []
        )
        # 环境变量适用于容器 secret，优先于落盘值。
        if os.environ.get("GITHUB_BRIDGE_TOKEN"):
            data["github_token"] = os.environ["GITHUB_BRIDGE_TOKEN"]
        self.value = Settings(
            **{key: value for key, value in data.items() if key in valid_keys}
        )
        self.value.validate()
        if changed:
            backup = (
                data_dir / f"settings.before-preset-cleanup.{secrets.token_hex(6)}.json"
            )
            secure_write(backup, original)
        if (
            original is None
            or changed
            or data.get("_schema_version") != SETTINGS_SCHEMA_VERSION
        ):
            self._persist(self.value)

    @property
    def configuration_warning(self) -> str:
        if not self.cleared_legacy_presets:
            return ""
        labels = {
            "github_owner": "GitHub 用户名",
            "issue_repositories": "Issue 允许列表",
            "target_umo": "私聊目标",
        }
        fields_text = "、".join(labels[key] for key in self.cleared_legacy_presets)
        return f"已清理旧版内置预设（{fields_text}）并暂停监控，原配置已备份。请填写自己的配置并保存后再启用。"

    def _persist(self, value: Settings, notices: list[str] | None = None) -> None:
        data = asdict(value)
        data["_schema_version"] = SETTINGS_SCHEMA_VERSION
        data["_cleared_legacy_presets"] = (
            self.cleared_legacy_presets if notices is None else notices
        )
        if os.environ.get("GITHUB_BRIDGE_TOKEN"):
            data["github_token"] = ""
        secure_write(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def update(self, patch: dict[str, Any]) -> Settings:
        allowed = {field.name for field in fields(Settings)}
        if set(patch) - allowed - {"clear_token"}:
            raise ValueError("包含不支持的配置字段")
        if os.environ.get("GITHUB_BRIDGE_TOKEN") and patch.get("github_token"):
            raise ValueError("Token 由环境变量管理，请在容器环境中修改")
        updated = asdict(self.value)
        for key, value in patch.items():
            if key in allowed and not (key == "github_token" and value == ""):
                updated[key] = value.strip() if isinstance(value, str) else value
        if patch.get("clear_token") is True:
            if os.environ.get("GITHUB_BRIDGE_TOKEN"):
                raise ValueError("Token 由环境变量管理，请在容器环境中清除")
            updated["github_token"] = ""
        value = Settings(**updated)
        value.validate()
        notices = [key for key in self.cleared_legacy_presets if key not in patch]
        self._persist(value, notices)
        self.value = value
        self.cleared_legacy_presets = notices
        return value
