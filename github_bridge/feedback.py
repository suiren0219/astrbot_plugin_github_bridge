from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Draft:
    code: str
    sender: str
    repo: str
    title: str
    body: str
    expires: float


class Drafts:
    def __init__(self):
        self.items: dict[str, Draft] = {}

    def create(self, sender: str, repo: str, title: str, body: str) -> Draft:
        self.items = {key: draft for key, draft in self.items.items() if draft.expires > time.time()}
        if len(self.items) >= 50:
            raise ValueError("待确认草稿过多，请先提交或等待旧草稿过期")
        if not title.strip() or len(title) > 256 or not body.strip() or len(body) > 60000:
            raise ValueError("标题须为 1–256 字，正文须为 1–60000 字")
        code = secrets.token_hex(3)
        draft = Draft(code, sender, repo, redact(title.strip()), redact(body.strip()), time.time() + 900)
        self.items[code] = draft
        return draft

    def pop(self, code: str, sender: str) -> Draft:
        draft = self.items.get(code)
        if not draft or draft.expires <= time.time() or draft.sender != sender:
            raise ValueError("草稿不存在、已过期或不属于当前私聊")
        return self.items.pop(code)


def redact(text: str) -> str:
    text = re.sub(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+", "[凭据已隐藏]", text)
    text = re.sub(r"(?im)((?:api[_-]?key|access[_-]?token|authorization|password|密码|token)\s*[:=：]\s*)[^\s,;]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+", r"\1[已隐藏]", text)
    return text


def group_context(payload: dict[str, Any], limit: int = 20) -> str:
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError("平台返回的群历史格式不正确")
    lines = []
    for message in messages[-limit:]:
        if not isinstance(message, dict):
            continue
        content = message.get("message", [])
        if isinstance(content, str):
            text = re.sub(r"\[CQ:[^\]]*\]", "[附件]", content)
        elif isinstance(content, list):
            text = "".join(str(segment.get("data", {}).get("text", "")) for segment in content if isinstance(segment, dict) and segment.get("type") == "text")
        else:
            continue
        text = redact(text).strip()[:500]
        if text:
            lines.append(f"消息 {len(lines) + 1}：{text}")
    if not lines:
        raise ValueError("没有读到可用于反馈的文本消息；图片内容需手动补充")
    return "\n\n".join(lines)[-8000:]
