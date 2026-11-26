"""PR 更新时间窗口采集，以及绑定通知版本的有限代码差异上下文。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .feedback import redact
from .github import GitHubError, _date, _id, _iso, _sha
from .models import Event

if TYPE_CHECKING:
    from .github import GitHubClient

_VERSION = 1
_KEY = re.compile(
    r"github:([1-9][0-9]{0,19}):pull:([1-9][0-9]{0,19}):"
    r"([0-9a-f]{40,64}):([0-9a-f]{40,64}):([0-9a-f]{64}):([0-9a-f]{24})"
)
_MAX_FILES = 20
_PATCH_LIMIT = 1800
_CONTEXT_LIMIT = 16000


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _number(value: Any) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise GitHubError("GitHub PR 缺少有效编号，未推进游标")
    return value


def _record(pull: Any) -> dict:
    if not isinstance(pull, dict):
        raise GitHubError("GitHub PR 响应无效，未推进游标")
    number = _number(pull.get("number"))
    head, base = pull.get("head"), pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise GitHubError("GitHub PR 缺少提交版本，未推进游标")
    title, body = pull.get("title", ""), pull.get("body")
    body = "" if body is None else body
    draft = pull.get("draft", False)
    if (
        not isinstance(title, str)
        or not isinstance(body, str)
        or type(draft) is not bool
        or pull.get("state") not in ("open", "closed")
    ):
        raise GitHubError("GitHub PR 信息格式无效，未推进游标")
    merged = pull.get("merged_at")
    if merged is not None:
        merged = _iso(_date(merged))
        if pull["state"] != "closed":
            raise GitHubError("GitHub PR 合并状态不一致，未推进游标")
    value = {
        "number": number,
        "head_sha": _sha(head.get("sha")),
        "base_sha": _sha(base.get("sha")),
        "state": pull["state"],
        "merged_at": merged,
        "draft": draft,
        "title_hash": _digest(title),
        "body_hash": _digest(body),
        "updated_at": _iso(_date(pull.get("updated_at"))),
    }
    if pull.get("created_at") is not None:
        _date(pull["created_at"])
    return {**value, "fingerprint": _digest(value)}


class PullRequests:
    def __init__(self, client: GitHubClient):
        self.client = client

    def _name(self, value: Any) -> str:
        name = self.client._name(value)
        if name.split("/", 1)[
            0
        ].casefold() != self.client.settings.identity or self.client.settings.excludes(
            name
        ):
            raise GitHubError("拒绝访问配置 owner 之外或已排除的 PR 仓库")
        return name

    def _text(self, value: str, limit: int) -> tuple[str, bool]:
        # 先完整清理再截断，避免凭据只剩一部分时逃过清理。
        text = redact(self.client._safe(value, max(2000, len(value) * 12 + 100)))
        cut = len(text) > limit
        return (text[: limit - 1] + "…" if cut else text), cut

    async def collect(
        self, repo: dict, old: dict | None, sampled_at: str
    ) -> tuple[dict, list[Event]]:
        _id(repo)
        name = self._name(repo.get("full_name"))
        sampled = _date(sampled_at)
        if old is not None and not isinstance(old, dict):
            raise GitHubError("PR 快照格式无效，未推进游标")
        # 老版本无 pulls source、重新启用、尚无此版本快照时都静默建立基线。
        baseline = (
            old
            if old
            and old.get("enabled") is not False
            and old.get("version") == _VERSION
            else None
        )
        since = sampled - timedelta(seconds=60)
        revision, previous = 1, {}
        if baseline is not None:
            cutoff = _date(baseline.get("since"))
            revision = baseline.get("revision")
            previous = baseline.get("seen")
            if (
                type(revision) is not int
                or revision < 1
                or not isinstance(previous, dict)
                or sampled < cutoff
            ):
                raise GitHubError("PR 快照游标无效，未推进游标")
            for number, item in previous.items():
                if (
                    not isinstance(item, dict)
                    or str(_number(item.get("number"))) != number
                    or not isinstance(item.get("fingerprint"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["fingerprint"])
                ):
                    raise GitHubError("PR 快照指纹无效，未推进游标")
            revision += 1
            since = max(since, cutoff)
        else:
            cutoff = since

        found: dict[str, tuple[dict, dict]] = {}
        visited: dict[int, str] = {}
        last_updated = None
        async for page in self.client._pages(
            f"/repos/{name}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc"},
        ):
            if not isinstance(page, list):
                raise GitHubError("GitHub PR 列表响应无效，未推进游标")
            reached_history = False
            for pull in page:
                item = _record(pull)
                number = item["number"]
                updated = _date(item["updated_at"])
                if number in visited:
                    message = (
                        "PR 分页出现冲突版本"
                        if visited[number] != item["fingerprint"]
                        else "PR 分页出现重复编号，可能发生分页移动"
                    )
                    raise GitHubError(message + "，未推进游标")
                visited[number] = item["fingerprint"]
                if last_updated is not None and updated > last_updated:
                    raise GitHubError("PR 分页更新时间顺序改变，未推进游标")
                last_updated = updated
                if updated < cutoff:
                    reached_history = True
                else:
                    found[str(number)] = (item, pull)
            # 必须严格早于 since；同秒边界的下一页仍需读取。
            if reached_history:
                break

        if baseline is not None and previous.keys() - found.keys():
            raise GitHubError("PR 窗口内已见编号缺失，无法确认分页完整性；未推进游标")
        state = {
            "version": _VERSION,
            "since": _iso(since),
            "revision": revision,
            "seen": {
                number: item
                for number, (item, _) in found.items()
                if _date(item["updated_at"]) >= since
            },
        }
        events = []
        if baseline is not None:
            for number, (item, pull) in sorted(
                found.items(),
                key=lambda entry: (entry[1][0]["updated_at"], int(entry[0])),
            ):
                before = previous.get(number)
                if before and before["fingerprint"] == item["fingerprint"]:
                    continue
                action = self._action(item, before, pull, cutoff)
                description, _ = self._text(pull.get("body") or "", 1200)
                title, _ = self._text(pull.get("title", ""), 200)
                digest = _digest({"revision": revision, "pull": item})[:24]
                events.append(
                    self.client._event(
                        repo,
                        "pull-request",
                        f"pull:{number}:{item['head_sha']}:{item['base_sha']}:"
                        f"{item['fingerprint']}:{digest}",
                        f"{action} PR #{number} {title}",
                        f"head SHA: {item['head_sha']}\nbase SHA: {item['base_sha']}\n"
                        f"状态: {item['state']}；草稿: {item['draft']}\n{description}",
                        item["updated_at"],
                        f"https://github.com/{name}/pull/{number}",
                    )
                )
        return state, events

    @staticmethod
    def _action(item: dict, before: dict | None, pull: dict, cutoff: Any) -> str:
        if item["merged_at"] and (
            before
            and item["merged_at"] != before["merged_at"]
            or not before
            and _date(item["merged_at"]) >= cutoff
        ):
            return "合并"
        if before and item["state"] != before["state"]:
            return "重开" if item["state"] == "open" else "关闭"
        if before and any(
            item[key] != before.get(key) for key in ("head_sha", "base_sha")
        ):
            return "更新代码"
        if before and any(
            item[key] != before.get(key) for key in ("title_hash", "body_hash", "draft")
        ):
            return "修改信息"
        if not before:
            if (
                item["state"] == "closed"
                and pull.get("closed_at")
                and _date(pull["closed_at"]) >= cutoff
            ):
                return "关闭"
            if pull.get("created_at") and _date(pull["created_at"]) >= cutoff:
                return "新建"
        return "活动更新"

    async def _repository(self, name: str, repo_id: int) -> None:
        data, _ = await self.client._request("GET", f"/repos/{name}")
        if (
            not isinstance(data, dict)
            or _id(data) != repo_id
            or self._name(data.get("full_name")).casefold() != name.casefold()
        ):
            raise GitHubError("PR 仓库身份已变化，取消代码分析")
        if (
            not self.client.settings.ai_summary_private
            and data.get("private") is not False
        ):
            raise GitHubError("仓库当前不是已确认的公开仓库，取消代码分析")

    async def _detail(
        self,
        name: str,
        repo_id: int,
        number: int,
        head: str,
        base: str,
        fingerprint: str,
    ) -> dict:
        data, _ = await self.client._request("GET", f"/repos/{name}/pulls/{number}")
        item = _record(data)
        if item["number"] != number:
            raise GitHubError("PR 编号不一致，取消代码分析")
        base_repo = data["base"].get("repo")
        if base_repo is not None and (
            not isinstance(base_repo, dict)
            or self._name(base_repo.get("full_name")).casefold() != name.casefold()
            or ("id" in base_repo and _id(base_repo) != repo_id)
        ):
            raise GitHubError("PR 目标仓库不一致，取消代码分析")
        if item["head_sha"] != head or item["base_sha"] != base:
            raise GitHubError("PR head/base SHA 已变化，取消旧版本代码分析")
        if item["fingerprint"] != fingerprint:
            raise GitHubError("PR 描述或状态等版本信息已变化，取消旧版本代码分析")
        return data

    def _file(self, data: Any) -> dict:
        if not isinstance(data, dict):
            raise GitHubError("GitHub PR 文件响应无效")
        filename, status = data.get("filename"), data.get("status")
        previous = data.get("previous_filename", "")
        patch = data.get("patch")
        if (
            not isinstance(filename, str)
            or not filename
            or not isinstance(status, str)
            or not status
            or not isinstance(previous, str)
            or (patch is not None and not isinstance(patch, str))
            or any(
                type(data.get(key)) is not int or data[key] < 0
                for key in ("additions", "deletions")
            )
        ):
            raise GitHubError("GitHub PR 文件字段无效")
        filename, cut_name = self._text(filename, 400)
        previous, cut_previous = self._text(previous, 400)
        status, cut_status = self._text(status, 40)
        patch, cut_patch = self._text(patch or "", _PATCH_LIMIT)
        return {
            "filename": filename,
            "previous_filename": previous,
            "status": status,
            "additions": data["additions"],
            "deletions": data["deletions"],
            "patch": patch,
            "missing_patch": not bool(data.get("patch")),
            "truncated": cut_name or cut_previous or cut_status or cut_patch,
        }

    async def context(self, event: dict) -> dict:
        if not isinstance(event, dict) or event.get("kind") != "pull-request":
            raise GitHubError("代码分析仅接受 PR 动态")
        name = self._name(event.get("repo"))
        key = event.get("event_key", event.get("key"))
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if not match:
            raise GitHubError("PR 动态缺少有效的版本标识")
        repo_id, number = int(match[1]), _number(int(match[2]))
        head, base = _sha(match[3]), _sha(match[4])
        fingerprint = match[5]
        canonical = f"https://github.com/{name}/pull/{number}"
        if event.get("url") != canonical:
            raise GitHubError("PR 链接与仓库或编号不一致，拒绝代码分析")
        await self._repository(name, repo_id)
        detail = await self._detail(name, repo_id, number, head, base, fingerprint)
        url = self.client._url(f"/repos/{name}/pulls/{number}/files")
        url += "?" + urlencode({"per_page": _MAX_FILES})
        files, filenames = [], set()
        truncated = False
        for _ in range(min(3, self.client.settings.max_pages)):
            data, headers = await self.client._request("GET", url)
            if not isinstance(data, list):
                raise GitHubError("GitHub PR 文件列表响应无效")
            next_url = self.client._next(headers.get("link", ""), url)
            for raw in data:
                item = self._file(raw)
                if raw["filename"] in filenames:
                    raise GitHubError("PR 文件分页出现重复，无法确认差异完整性")
                filenames.add(raw["filename"])
                if len(files) < _MAX_FILES:
                    files.append(item)
                else:
                    truncated = True
            if not next_url:
                break
            if len(files) >= _MAX_FILES:
                truncated = True
                break
            url = next_url
        else:
            truncated = True
        expected = detail.get("changed_files")
        if expected is not None:
            if type(expected) is not int or expected < 0 or expected < len(filenames):
                raise GitHubError("PR 文件总数不一致，取消代码分析")
            truncated = truncated or expected > len(files)
        # files 是可变端点；读取后再次核实，不能分析排队后或分页中已变化的版本。
        detail = await self._detail(name, repo_id, number, head, base, fingerprint)
        if not self.client.settings.ai_summary_private:
            await self._repository(name, repo_id)
        description, cut_description = self._text(detail.get("body") or "", 2000)
        context = {
            "url": canonical,
            "number": number,
            "head_sha": head,
            "base_sha": base,
            "description": description,
            "state": "merged" if detail.get("merged_at") else detail["state"],
            "scope": "当前PR累计差异",
            "files": files,
            "truncated": truncated
            or cut_description
            or any(f["truncated"] for f in files),
            "missing_patch": any(f["missing_patch"] for f in files),
            "limitation": "",
        }

        def limitation() -> str:
            notes = ["仅包含当前 PR 累计差异，不代表本次推送增量，也未执行代码或测试。"]
            if context["truncated"]:
                notes.append("受文件数、分页或文本长度限制，仅提供部分内容。")
            if context["missing_patch"]:
                notes.append(
                    "GitHub 未提供部分文件的 patch（可能为二进制或大文件），无法审查其内容。"
                )
            return "".join(notes)

        context["limitation"] = limitation()
        while len(json.dumps(context, ensure_ascii=False)) > _CONTEXT_LIMIT and files:
            files.pop()
            context["truncated"] = True
            context["limitation"] = limitation()
        return context
