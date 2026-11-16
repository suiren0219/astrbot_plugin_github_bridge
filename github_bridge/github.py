"""固定 GitHub REST origin 的客户端；一次 observe 成功后才能提交其快照。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlencode, unquote, urljoin, urlsplit

import aiohttp

from .models import Event, Observation
from .settings import REPO_PATTERN, Settings


API = "https://api.github.com"
SCHEMA = 1
_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
_METADATA = (
    "full_name", "default_branch", "description", "stargazers_count", "forks_count",
    "open_issues_count", "visibility", "private", "archived", "disabled", "homepage",
)


class GitHubError(Exception):
    """message 永不包含服务端正文、请求头或 Token；retry_at 为 Unix 秒。"""

    def __init__(self, message: str, retry_at: float | None = None, status: int | None = None):
        self.message = message
        self.retry_at = retry_at
        self.status = status
        super().__init__(message)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise GitHubError("GitHub 返回的事件时间无效，未推进游标") from None


def _id(item: dict) -> int:
    value = item.get("id")
    if type(value) is not int or value <= 0:
        raise GitHubError("GitHub 返回的数据缺少有效 ID，未推进游标")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise GitHubError("GitHub 返回的提交 SHA 无效，未推进游标")
    return value.lower()


class GitHubClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._retry_at = 0.0
        self._failures = 0
        self.rate_limit: dict[str, int | None] = {"remaining": None, "limit": None, "reset": None}
        self._visibility: dict[str, Any] = {}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _safe(self, value: Any, limit: int = 2000) -> str:
        text = str(value or "")
        token = self.settings.github_token.strip()
        if token:
            text = text.replace(token, "[REDACTED]")
        text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
        return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"

    @staticmethod
    def _url(path: str) -> str:
        # 绝不把 credentials 带到重定向或 Link 指定的外站。
        if not isinstance(path, str) or any(ord(c) <= 32 for c in path) or "\\" in path:
            raise GitHubError("拒绝不安全的 GitHub API 地址")
        url = API + path if path.startswith("/") and not path.startswith("//") else path
        try:
            parts = urlsplit(url)
            if (parts.scheme != "https" or parts.netloc != "api.github.com"
                    or parts.username or parts.password or parts.fragment):
                raise ValueError
            decoded = unquote(parts.path)
            if not decoded.startswith("/") or "\\" in decoded or any(x in (".", "..") for x in decoded.split("/")):
                raise ValueError
            if any(ord(c) < 32 for c in unquote(url)):
                raise ValueError
        except ValueError:
            raise GitHubError("拒绝不安全的 GitHub API 地址") from None
        return url

    def _next(self, link: str, current: str) -> str | None:
        if not link:
            return None
        targets: list[str] = []
        # GitHub Link 的 URI 内没有裸逗号；分割只发生于下一个 <URI> 之前。
        for section in re.split(r",\s*(?=<)", link):
            match = re.fullmatch(r"\s*<([^>]+)>\s*((?:;[^<>]*)?)", section)
            if not match:
                raise GitHubError("GitHub 分页 Link 格式无效，无法确认完整性")
            rel = re.search(r';\s*rel\s*=\s*(?:"([^"]+)"|([^;\s]+))', match[2], re.I)
            if rel and "next" in (rel[1] or rel[2]).split():
                targets.append(self._url(urljoin(current, match[1])))
        if len(targets) > 1:
            raise GitHubError("GitHub 分页含多个 next，无法确认完整性")
        if not targets:
            return None
        target = targets[0]
        old, new = urlsplit(current), urlsplit(target)
        if old.path != new.path:
            raise GitHubError("GitHub 分页路径改变，拒绝继续")
        before, after = parse_qs(old.query), parse_qs(new.query)
        try:
            page_before = int(before.get("page", ["1"])[0])
            page_after = int(after.get("page", [""])[0])
            if len(after.get("page", [])) != 1 or page_after != page_before + 1:
                raise ValueError
        except ValueError:
            raise GitHubError("GitHub 分页跳页或循环，无法确认完整性") from None
        before.pop("page", None)
        after.pop("page", None)
        if before != after:
            raise GitHubError("GitHub 分页查询条件改变，拒绝继续")
        return target

    def _backoff(self, headers: dict[str, str]) -> float:
        self._failures += 1
        now = time.time()
        retry = now + min(900, 30 * 2 ** min(self._failures - 1, 5))
        value = headers.get("retry-after", "")
        try:
            retry = max(retry, now + float(value))
        except ValueError:
            try:
                retry = max(retry, parsedate_to_datetime(value).timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
        if headers.get("x-ratelimit-remaining") == "0":
            try:
                retry = max(retry, float(headers["x-ratelimit-reset"]) + 1)
            except (KeyError, ValueError):
                pass
        self._retry_at = retry
        return retry

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json_body: dict | None = None) -> tuple[Any, dict[str, str]]:
        url = self._url(path)
        if self._retry_at > time.time():
            raise GitHubError("GitHub 请求处于退避期，请稍后重试", self._retry_at)
        token = self.settings.github_token.strip()
        if token and any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise GitHubError("GitHub Token 格式无效")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "astrbot-plugin-github-bridge"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if self._session is None:
            self._session = aiohttp.ClientSession(trust_env=False)
        try:
            async with self._session.request(
                method, url, params=params, json=json_body, headers=headers,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout),
            ) as response:
                result_headers = {k.lower(): v for k, v in response.headers.items()}
                for key in self.rate_limit:
                    value = result_headers.get("x-ratelimit-" + key)
                    if value is not None:
                        try:
                            self.rate_limit[key] = int(value)
                        except ValueError:
                            pass
                status = response.status
                if 300 <= status < 400:
                    raise GitHubError("GitHub 返回重定向，已拒绝跟随；请刷新仓库名称", status=status)
                if status == 429 or status == 403:
                    # 403 也可能是 secondary limit，没有标准额度头；保守退避，且不回显正文。
                    retry = self._backoff(result_headers)
                    message = "GitHub 请求受限，请等待额度恢复或检查 Token 权限"
                    raise GitHubError(message, retry, status)
                if status >= 500:
                    raise GitHubError("GitHub 服务暂时不可用", self._backoff(result_headers), status)
                if status >= 400:
                    messages = {401: "GitHub 身份验证失败，请检查 Token", 404: "GitHub 资源不可见、已删除或 Token 缺少权限",
                                409: "GitHub 仓库为空或资源状态冲突", 422: "GitHub 拒绝请求参数或当前资源状态"}
                    raise GitHubError(messages.get(status, "GitHub 请求失败"), status=status)
                try:
                    data = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    raise GitHubError("GitHub 返回无效 JSON，未推进游标") from None
                self._failures = 0
                return data, result_headers
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            # 不传播 aiohttp 异常：其中可能含 URL、请求头或服务端正文。
            retry = self._backoff({})
        else:
            raise AssertionError("unreachable")
        raise GitHubError("GitHub 网络连接失败或请求超时", retry) from None

    async def _pages(self, path: str, params: dict | None = None) -> AsyncIterator[Any]:
        query = {"per_page": 100, **(params or {})}
        url = self._url(path) + "?" + urlencode(query)
        visited: set[str] = set()
        for _ in range(self.settings.max_pages):
            if url in visited:
                raise GitHubError("GitHub 分页循环，未推进游标")
            visited.add(url)
            data, headers = await self._request("GET", url)
            next_url = self._next(headers.get("link", ""), url)
            yield data
            if next_url is None:
                return
            url = next_url
        raise GitHubError("GitHub 分页超过 max_pages，采集不完整；请增大上限后重试")

    async def _list(self, path: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        async for page in self._pages(path, params):
            if not isinstance(page, list) or any(not isinstance(x, dict) for x in page):
                raise GitHubError("GitHub 列表响应无效，未推进游标")
            items.extend(page)
        return items

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str) or not REPO_PATTERN.fullmatch(value) or any(x in (".", "..") for x in value.split("/")):
            raise GitHubError("GitHub 仓库名称无效")
        return value

    async def list_repositories(self) -> list[dict]:
        owner = self.settings.github_owner
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
            raise GitHubError("GitHub owner 格式无效")
        actor: dict = {}
        if self.settings.github_token.strip():
            actor, _ = await self._request("GET", "/user")
            if not isinstance(actor, dict) or not isinstance(actor.get("login"), str):
                raise GitHubError("无法核实 Token 所属用户")
        profile, _ = await self._request("GET", f"/users/{owner}")
        if not isinstance(profile, dict) or str(profile.get("login", "")).casefold() != owner.casefold():
            raise GitHubError("GitHub owner 身份核实失败")
        authenticated = bool(actor)
        if profile.get("type") == "Organization":
            path, params = f"/orgs/{owner}/repos", {"type": "all"}
            visibility = "token-accessible" if authenticated else "public-only"
            note = "组织仅包含 Token 获授权可见的仓库；请授予所需私有仓库访问权限。" if authenticated else "未配置 Token，只能发现组织公开仓库。"
        elif profile.get("type") == "User":
            if authenticated and actor["login"].casefold() != owner.casefold():
                raise GitHubError("Token 所属用户与配置 owner 不一致，无法完整发现该用户的私有仓库")
            path = "/user/repos" if authenticated else f"/users/{owner}/repos"
            params = {"affiliation": "owner", "visibility": "all"} if authenticated else {"type": "owner"}
            visibility = "token-accessible" if authenticated else "public-only"
            note = "已核实 Token owner；私有仓库仍受 Token scopes、细粒度授权及 SSO 限制。" if authenticated else "未配置 Token，只能发现公开仓库。"
        else:
            raise GitHubError("不支持的 GitHub owner 类型")
        repositories = await self._list(path, params)
        unique: dict[int, dict] = {}
        for repo in repositories:
            login = (repo.get("owner") or {}).get("login", "")
            if not isinstance(login, str) or login.casefold() != owner.casefold():
                continue
            full_name = self._name(repo.get("full_name"))
            if full_name.split("/")[0].casefold() != owner.casefold():
                raise GitHubError("GitHub 仓库 owner 信息不一致")
            if not self.settings.excludes(full_name):
                unique[_id(repo)] = repo
        self._visibility = {"owner": owner, "login": actor.get("login", owner) if actor else owner,
                            "authenticated": authenticated, "visibility": visibility,
                            "private_visibility_complete": False, "visibility_note": note}
        return sorted(unique.values(), key=lambda item: item["full_name"].casefold())

    async def test_connection(self) -> dict:
        repos = await self.list_repositories()
        return {"ok": True, **self._visibility, "repository_count": len(repos),
                "private_repository_count": sum(bool(repo.get("private")) for repo in repos),
                "rate_limit": dict(self.rate_limit)}

    async def create_issue(self, full_name: str, title: str, body: str) -> dict:
        name = self._name(full_name)
        if not self.settings.github_token.strip():
            raise GitHubError("创建 Issue 需要配置 GitHub Token")
        if not self.settings.allows_issue(name) or self.settings.excludes(name):
            raise GitHubError("此仓库不在允许创建 Issue 的范围内")
        if not isinstance(title, str) or not title.strip() or len(title) > 256:
            raise GitHubError("Issue 标题必须为 1–256 个字符")
        if not isinstance(body, str) or len(body) > 65536:
            raise GitHubError("Issue 正文过长或格式无效")
        result, _ = await self._request("POST", f"/repos/{name}/issues", json_body={"title": title.strip(), "body": body})
        if not isinstance(result, dict) or not result.get("number") or "pull_request" in result:
            raise GitHubError("GitHub 创建 Issue 的响应无效；请核实仓库后再重试")
        # 不自动重试 POST；超时后无法证明服务端没有创建成功。
        return result

    def _event(self, repo: dict, kind: str, suffix: str, title: Any, body: Any,
               occurred_at: str, url: str = "", legacy_keys: tuple[str, ...] = ()) -> Event:
        return Event(key=f"github:{_id(repo)}:{suffix}", repo=self._safe(repo["full_name"], 300),
                     kind=kind, title=self._safe(title, 300), body=self._safe(body, 3000),
                     url=self._safe(url, 1000), occurred_at=occurred_at, legacy_keys=legacy_keys)

    async def observe(self, repo: dict, previous: dict | None) -> Observation:
        repo_id = _id(repo)
        name = self._name(repo.get("full_name"))
        if name.split("/")[0].casefold() != self.settings.identity:
            raise GitHubError("拒绝采集配置 owner 之外的仓库")
        sampled_at = _iso(datetime.now(timezone.utc))
        # 列表项可能过时；ID 端点在 rename 后仍可定位同一仓库。
        current, _ = await self._request("GET", f"/repositories/{repo_id}")
        if not isinstance(current, dict) or _id(current) != repo_id:
            raise GitHubError("GitHub 仓库身份改变，未推进游标")
        self._name(current.get("full_name"))
        if current["full_name"].split("/")[0].casefold() != self.settings.identity:
            raise GitHubError("仓库已转移到其他 owner，停止采集")
        if previous is not None and (previous.get("schema") != SCHEMA or previous.get("repo_id") != repo_id):
            raise GitHubError("快照版本或仓库 ID 不匹配，需要重新建立基线")
        old_sources = previous.get("sources", {}) if previous else {}
        revision = int(previous.get("revision", 0)) + 1 if previous else 1
        sources: dict[str, dict] = {}
        events: list[Event] = []
        for source, enabled in (("commits", self.settings.monitor_commits),
                                ("releases", self.settings.monitor_releases),
                                ("issues", self.settings.monitor_issues),
                                ("issue_events", self.settings.monitor_issues),
                                ("comments", self.settings.monitor_issues)):
            old = old_sources.get(source)
            baseline = old if old and old.get("enabled") else None
            if not enabled or source in {"issues", "issue_events", "comments"} and current.get("has_issues") is False:
                sources[source] = {"enabled": False}
                continue
            collector = getattr(self, "_collect_" + source)
            state, discovered = await collector(current, baseline, sampled_at)
            sources[source] = {"enabled": True, **state}
            if baseline is not None:
                events.extend(discovered)
        metadata = {key: current.get(key) for key in _METADATA}
        old_metadata = old_sources.get("metadata", {})
        if self.settings.monitor_metadata:
            if old_metadata.get("enabled") and metadata != old_metadata.get("values"):
                changed = {key: {"before": old_metadata["values"].get(key), "after": metadata[key]}
                           for key in _METADATA if old_metadata["values"].get(key) != metadata[key]}
                digest = hashlib.sha256(json.dumps({"revision": revision, "changes": changed,
                    "updated_at": current.get("updated_at")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
                events.append(self._event(current, "metadata", f"metadata:{digest}", "仓库信息变化",
                    json.dumps(changed, ensure_ascii=False), current.get("updated_at") or sampled_at,
                    f"https://github.com/{current['full_name']}"))
            sources["metadata"] = {"enabled": True, "values": metadata}
        else:
            sources["metadata"] = {"enabled": False}
        # 此前从未修改 previous；任一请求失败时不会返回部分状态。
        return Observation(snapshot={"schema": SCHEMA, "repo_id": repo_id, "full_name": current["full_name"],
            "revision": revision, "sampled_at": sampled_at, "sources": sources},
            events=list({event.key: event for event in events}.values()))

    def _commit_event(self, repo: dict, commit: dict, sampled_at: str) -> Event:
        sha = _sha(commit.get("sha"))
        details = commit.get("commit") or {}
        message = details.get("message") or sha
        author = details.get("author") or {}
        return self._event(repo, "commit", f"commit:{sha}", str(message).split("\n", 1)[0], message,
            author.get("date") or sampled_at, f"https://github.com/{repo['full_name']}/commit/{sha}",
            (f"{repo['full_name'].casefold()}@{sha}",))

    async def _collect_commits(self, repo: dict, old: dict | None, sampled_at: str) -> tuple[dict, list[Event]]:
        branch = repo.get("default_branch")
        if not branch:
            return {"branch": branch, "head": None}, []
        path = f"/repos/{repo['full_name']}"
        try:
            commits, _ = await self._request("GET", path + "/commits", params={"sha": branch, "per_page": 1})
        except GitHubError as exc:
            if exc.status == 409 and (old is None or old.get("head") is None):
                return {"branch": branch, "head": None}, []
            raise
        if not isinstance(commits, list) or len(commits) != 1 or not isinstance(commits[0], dict):
            raise GitHubError("无法读取默认分支 HEAD，未推进提交游标")
        head = _sha(commits[0].get("sha"))
        state = {"branch": branch, "head": head}
        if old is None or old.get("branch") != branch or old.get("head") == head:
            return state, []
        base = old.get("head")
        if base is None:
            # 空仓库首次出现提交：沿完整历史回溯；有 merge 时显式要求 compare 基线。
            collected = await self._walk_commits(path, head, None)
        else:
            base = _sha(base)
            collected = await self._compare(path, base, head)
            if collected is None:
                collected = await self._walk_commits(path, head, base)
        return state, [self._commit_event(repo, commit, sampled_at) for commit in collected]

    async def _compare(self, path: str, base: str, head: str) -> list[dict] | None:
        collected: dict[str, dict] = {}
        total: int | None = None
        try:
            async for page in self._pages(path + f"/compare/{base}...{head}"):
                if not isinstance(page, dict) or page.get("status") != "ahead":
                    raise GitHubError("默认分支历史被改写或发生分叉，无法确认新增提交；需重新建立基线")
                if (page.get("base_commit") or {}).get("sha") != base:
                    raise GitHubError("GitHub compare 基准不一致，未推进提交游标")
                if type(page.get("total_commits")) is not int or page["total_commits"] < 1:
                    return None
                if total is not None and total != page["total_commits"]:
                    raise GitHubError("GitHub compare 分页总数不一致")
                total = page["total_commits"]
                entries = page.get("commits")
                if not isinstance(entries, list) or any(not isinstance(c, dict) for c in entries):
                    raise GitHubError("GitHub compare 响应无效")
                for commit in entries:
                    collected[_sha(commit.get("sha"))] = commit
        except GitHubError as exc:
            if exc.status in (404, 409, 422):
                return None
            raise
        if total != len(collected) or head not in collected or base in collected:
            return None
        return list(collected.values())

    async def _walk_commits(self, path: str, head: str, base: str | None) -> list[dict]:
        # compare 不可用时，仅线性 parent 链能证明完整性；merge 的额外祖先不能静默跳过。
        collected: list[dict] = []
        expected: str | None = head
        seen: set[str] = set()
        async for page in self._pages(path + "/commits", {"sha": head}):
            if not isinstance(page, list) or any(not isinstance(c, dict) for c in page):
                raise GitHubError("GitHub 提交历史响应无效")
            for commit in page:
                sha = _sha(commit.get("sha"))
                if sha in seen:
                    continue
                seen.add(sha)
                if sha != expected:
                    raise GitHubError("提交历史存在缺口，未推进提交游标")
                if sha == base:
                    return list(reversed(collected))
                parents = commit.get("parents")
                if not isinstance(parents, list) or len(parents) > 1:
                    raise GitHubError("compare 不完整且回溯遇到合并提交，无法证明完整性；请提高分页上限或重新建立基线")
                collected.append(commit)
                expected = _sha(parents[0].get("sha")) if parents else None
                if expected is None and base is None:
                    return list(reversed(collected))
        raise GitHubError("提交历史窗口内找不到前次 HEAD，可能发生强制推送或 API 截断；未推进游标")

    async def _collect_releases(self, repo: dict, old: dict | None, sampled_at: str) -> tuple[dict, list[Event]]:
        entries = await self._list(f"/repos/{repo['full_name']}/releases")
        seen = set(old.get("published", [])) if old else set()
        events: list[Event] = []
        for release in entries:
            if release.get("draft") or not release.get("published_at"):
                continue
            published = release["published_at"]
            _date(published)
            identity = f"{_id(release)}:{published}"
            if old is not None and identity not in seen:
                label = "预发布" if release.get("prerelease") else "发布"
                events.append(self._event(repo, "release", f"release:{identity}",
                    f"{label} {release.get('name') or release.get('tag_name') or release['id']}",
                    release.get("body", ""), published,
                    f"https://github.com/{repo['full_name']}/releases"))
            seen.add(identity)
        # 保留已删除 release 的身份，删除后重新出现不会重复通知。
        return {"published": sorted(seen)}, events

    @staticmethod
    def _dedup(items: list[dict]) -> dict[int, dict]:
        return {_id(item): item for item in items}

    @staticmethod
    def _anchor(items: dict[int, dict], old: dict | None, source: str) -> None:
        if old and old.get("anchor") is not None and old["anchor"] not in items:
            raise GitHubError(f"{source} 的历史锚点缺失：可能删除、窗口截断或分页缺失，未推进游标")

    def _since_state(self, items: dict[int, dict], old: dict | None, sampled_at: str) -> dict:
        if not items:
            return dict(old) if old else {"since": _iso(_date(sampled_at) - timedelta(seconds=60)),
                                          "high_id": 0, "anchor": None}
        latest = max(_date(item["created_at"]) for item in items.values())
        since = _iso(latest - timedelta(seconds=60))
        if old:
            since = max(since, old["since"])
        high = max(max(items), old.get("high_id", 0) if old else 0)
        return {"since": since, "high_id": high, "anchor": high}

    async def _collect_issues(self, repo: dict, old: dict | None, sampled_at: str) -> tuple[dict, list[Event]]:
        params: dict = {"state": "all", "sort": "created", "direction": "asc"}
        if old:
            params["since"] = old["since"]
        items = self._dedup(await self._list(f"/repos/{repo['full_name']}/issues", params))
        state = self._since_state(items, old, sampled_at)
        events = []
        for identity, issue in sorted(items.items()):
            created = issue.get("created_at")
            _date(created)
            if "pull_request" in issue or old is None or identity <= old.get("high_id", 0):
                continue
            if _date(created) < _date(old["since"]):
                raise GitHubError("Issue 新建超出历史窗口，未推进游标")
            number = issue.get("number")
            if type(number) is not int or number <= 0:
                raise GitHubError("GitHub Issue 缺少有效编号")
            events.append(self._event(repo, "issue", f"issue:{identity}", f"新建 Issue #{number} {issue.get('title', '')}",
                issue.get("body", ""), created, f"https://github.com/{repo['full_name']}/issues/{number}"))
        return state, events

    async def _issue(self, path: str, number: int, cache: dict[int, dict]) -> dict:
        if number not in cache:
            data, _ = await self._request("GET", path + f"/issues/{number}")
            if not isinstance(data, dict) or data.get("number") != number:
                raise GitHubError("无法确认 Issue/PR 身份，未推进游标")
            cache[number] = data
        return cache[number]

    async def _collect_issue_events(self, repo: dict, old: dict | None, sampled_at: str) -> tuple[dict, list[Event]]:
        path = f"/repos/{repo['full_name']}"
        # 该端点无 since，也未承诺排序：完整翻页后按 ID 去重，不依赖公开 Events API。
        items = self._dedup(await self._list(path + "/issues/events"))
        self._anchor(items, old, "Issue 生命周期")
        seen = set(old.get("ids", [])) if old else set()
        actions = {"closed": "关闭", "reopened": "重开", "renamed": "修改标题", "labeled": "添加标签",
                   "unlabeled": "移除标签", "assigned": "分配负责人", "unassigned": "移除负责人",
                   "locked": "锁定", "unlocked": "解锁", "milestoned": "设置里程碑",
                   "demilestoned": "移除里程碑", "transferred": "转移", "pinned": "置顶", "unpinned": "取消置顶"}
        events: list[Event] = []
        for identity, activity in sorted(items.items()):
            if old is None or identity in seen or activity.get("event") not in actions:
                continue
            issue = activity.get("issue")
            if not isinstance(issue, dict) or type(issue.get("number")) is not int:
                raise GitHubError("Issue 生命周期事件缺少 Issue 身份，未推进游标")
            number = issue["number"]
            if "pull_request" in issue:
                continue
            created = activity.get("created_at")
            _date(created)
            action = actions[activity["event"]]
            events.append(self._event(repo, "issue-event", f"issue-event:{identity}",
                f"{action} Issue #{number} {issue.get('title', '')}", activity["event"], created,
                f"https://github.com/{repo['full_name']}/issues/{number}"))
        return {"ids": sorted(items), "anchor": max(items) if items else None}, events

    async def _collect_comments(self, repo: dict, old: dict | None, sampled_at: str) -> tuple[dict, list[Event]]:
        path = f"/repos/{repo['full_name']}"
        params: dict = {"sort": "created", "direction": "asc"}
        if old:
            params["since"] = old["since"]
        items = self._dedup(await self._list(path + "/issues/comments", params))
        state = self._since_state(items, old, sampled_at)
        cache: dict[int, dict] = {}
        events: list[Event] = []
        for identity, comment in sorted(items.items()):
            if old is None or identity <= old.get("high_id", 0):
                continue
            created = comment.get("created_at")
            if _date(created) < _date(old["since"]):
                raise GitHubError("Issue 评论超出历史窗口，未推进游标")
            issue_url = self._url(str(comment.get("issue_url", "")))
            match = re.fullmatch(re.escape(path) + r"/issues/([1-9][0-9]*)", urlsplit(issue_url).path)
            if not match or urlsplit(issue_url).query:
                raise GitHubError("评论的 Issue 地址与仓库不一致，未推进游标")
            number = int(match[1])
            issue = await self._issue(path, number, cache)
            if "pull_request" in issue:
                continue
            events.append(self._event(repo, "comment", f"comment:{identity}",
                f"Issue #{number} 新评论 {issue.get('title', '')}", comment.get("body", ""), created,
                f"https://github.com/{repo['full_name']}/issues/{number}#issuecomment-{identity}"))
        return state, events
