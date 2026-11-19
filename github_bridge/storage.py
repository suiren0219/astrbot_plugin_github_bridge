from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import Event, Observation


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS repositories (
                owner TEXT NOT NULL, id TEXT NOT NULL, full_name TEXT NOT NULL,
                payload TEXT NOT NULL, snapshot TEXT, active INTEGER NOT NULL DEFAULT 1,
                last_check REAL, error TEXT NOT NULL DEFAULT '', PRIMARY KEY(owner,id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
                owner TEXT NOT NULL, repo TEXT NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL,
                occurred_at TEXT NOT NULL, target TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                sent_at REAL, last_error TEXT NOT NULL DEFAULT '',
                manual_retry INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS events_queue ON events(owner,target,status,next_attempt);
            CREATE TABLE IF NOT EXISTS event_summaries (
                event_id INTEGER PRIMARY KEY, summary TEXT NOT NULL,
                state TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_visibility (
                event_id INTEGER PRIMARY KEY, private INTEGER
            );
            CREATE TABLE IF NOT EXISTS legacy_seen (event_key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, holder TEXT, expires REAL);
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def meta(self, key: str, default: Any = None) -> Any:
        row = self.db.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES(?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def acquire(self, holder: str, ttl: int = 180) -> bool:
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO leases VALUES('poll',NULL,0)")
            changed = self.db.execute(
                "UPDATE leases SET holder=?,expires=? WHERE name='poll' AND (expires<? OR holder=?)",
                (holder, now + ttl, now, holder),
            ).rowcount
        return bool(changed)

    def release(self, holder: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM leases WHERE name='poll' AND holder=?", (holder,)
            )

    def recover_interrupted(self) -> int:
        # 上次在发送与记账之间退出时，无法确认 QQ 是否收到；由管理员手动处理，避免自动重复发。
        with self.db:
            return self.db.execute(
                "UPDATE events SET status='failed',manual_retry=1,last_error=? WHERE status='sending'",
                ("上次发送被中断，送达状态未知。请核对私聊后手动重试。",),
            ).rowcount

    def discover(self, owner: str, repos: list[dict[str, Any]]) -> None:
        with self.db:
            self.db.execute("UPDATE repositories SET active=0 WHERE owner=?", (owner,))
            for repo in repos:
                self.db.execute(
                    """
                    INSERT INTO repositories(owner,id,full_name,payload) VALUES(?,?,?,?)
                    ON CONFLICT(owner,id) DO UPDATE SET full_name=excluded.full_name,
                    payload=excluded.payload,active=1
                """,
                    (
                        owner,
                        str(repo["id"]),
                        repo["full_name"],
                        json.dumps(repo, ensure_ascii=False),
                    ),
                )

    def snapshot(self, owner: str, repo_id: Any) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT snapshot FROM repositories WHERE owner=? AND id=?",
            (owner, str(repo_id)),
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def _enqueue(
        self,
        owner: str,
        target: str,
        event: Event,
        status: str = "pending",
        ignore_legacy: bool = False,
        private: bool | None = None,
    ) -> bool:
        keys = (event.key, *event.legacy_keys)
        if not ignore_legacy and any(
            self.db.execute(
                "SELECT 1 FROM legacy_seen WHERE event_key=?", (key,)
            ).fetchone()
            for key in keys
        ):
            return False
        row = self.db.execute(
            """
            INSERT OR IGNORE INTO events(event_key,owner,repo,kind,title,body,url,occurred_at,target,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                event.key,
                owner,
                event.repo,
                event.kind,
                event.title,
                event.body,
                event.url,
                event.occurred_at,
                target,
                status,
                time.time(),
            ),
        )
        if row.rowcount:
            self.db.execute(
                "INSERT INTO event_visibility VALUES(?,?)", (row.lastrowid, private)
            )
            for alias in event.legacy_keys:
                self.db.execute("INSERT OR IGNORE INTO legacy_seen VALUES(?)", (alias,))
        return bool(row.rowcount)

    def record(
        self, owner: str, target: str, repo: dict[str, Any], observation: Observation
    ) -> int:
        count = 0
        with self.db:
            for event in observation.events:
                private = repo.get("private")
                count += self._enqueue(
                    owner,
                    target,
                    event,
                    private=private if type(private) is bool else None,
                )
            self.db.execute(
                """
                UPDATE repositories SET snapshot=?,last_check=?,error='' WHERE owner=? AND id=?
            """,
                (
                    json.dumps(observation.snapshot, ensure_ascii=False),
                    time.time(),
                    owner,
                    str(repo["id"]),
                ),
            )
        return count

    def reset_snapshot(self, owner: str, full_name: str) -> bool:
        with self.db:
            return bool(
                self.db.execute(
                    "UPDATE repositories SET snapshot=NULL,error='' WHERE owner=? AND full_name=? COLLATE NOCASE",
                    (owner, full_name),
                ).rowcount
            )

    def record_error(self, owner: str, repo_id: Any, error: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE repositories SET error=? WHERE owner=? AND id=?",
                (error[:500], owner, str(repo_id)),
            )

    def repositories(self, owner: str) -> list[dict[str, Any]]:
        result = []
        for row in self.db.execute(
            "SELECT * FROM repositories WHERE owner=? AND active=1 ORDER BY full_name COLLATE NOCASE",
            (owner,),
        ):
            repo = json.loads(row["payload"])
            keys = (
                "id",
                "full_name",
                "description",
                "private",
                "archived",
                "default_branch",
                "stargazers_count",
                "forks_count",
                "open_issues_count",
                "html_url",
            )
            result.append(
                {
                    **{key: repo.get(key) for key in keys},
                    "last_check": row["last_check"],
                    "error": row["error"],
                }
            )
        return result

    def queue(self, owner: str, target: str, limit: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                """
            SELECT * FROM events WHERE owner=? AND target=? AND status IN ('pending','failed')
            AND manual_retry=0 AND next_attempt<=? ORDER BY id LIMIT ?
        """,
                (owner, target, time.time(), limit),
            )
        ]

    def summary(self, event_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT summary,state FROM event_summaries WHERE event_id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def save_summary(self, event_id: int, text: str, state: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO event_summaries VALUES(?,?,?,?)",
                (event_id, text, state, time.time()),
            )

    def is_public_event(self, event: dict[str, Any], owner: str) -> bool:
        original = self.db.execute(
            "SELECT private FROM event_visibility WHERE event_id=?", (event["id"],)
        ).fetchone()
        current = self.db.execute(
            "SELECT payload FROM repositories WHERE owner=? AND full_name=? AND active=1",
            (owner, event["repo"]),
        ).fetchone()
        # 未知可见性（例如升级前队列/旧监控导入）保守处理，避免私有内容进入模型。
        return bool(
            original
            and original["private"] == 0
            and current
            and json.loads(current["payload"]).get("private") is False
        )

    @staticmethod
    def _event_ids(event_ids: list[int]) -> list[int]:
        unique = []
        seen = set()
        for event_id in event_ids:
            if type(event_id) is not int or event_id < 1:
                raise ValueError("通知 ID 必须是正整数")
            if event_id not in seen:
                unique.append(event_id)
                seen.add(event_id)
        return unique

    def mark_sending_many(self, event_ids: list[int]) -> int:
        ids = self._event_ids(event_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.db:
            changed = self.db.execute(
                f"""
                UPDATE events SET status='sending',attempts=attempts+1
                WHERE id IN ({placeholders})
                    AND status IN ('pending','failed') AND manual_retry=0
                """,
                ids,
            ).rowcount
            if changed != len(ids):
                raise RuntimeError("通知队列状态已改变，未能领取完整发送批次")
        return changed

    def mark_sent_many(self, event_ids: list[int]) -> int:
        ids = self._event_ids(event_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.db:
            changed = self.db.execute(
                f"""
                UPDATE events SET status='sent',sent_at=?,last_error=''
                WHERE id IN ({placeholders}) AND status='sending'
                """,
                [time.time(), *ids],
            ).rowcount
            if changed != len(ids):
                raise RuntimeError("通知队列状态已改变，未能完整记录发送成功")
        return changed

    def mark_failed_many(self, event_ids: list[int], error: str) -> int:
        ids = self._event_ids(event_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = time.time()
        with self.db:
            rows = self.db.execute(
                f"""
                SELECT id,attempts FROM events
                WHERE id IN ({placeholders}) AND status='sending'
                """,
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                raise RuntimeError("通知队列状态已改变，未能完整记录发送失败")
            updates = [
                (
                    error[:500],
                    now + min(3600, 30 * 2 ** min(row["attempts"], 7)),
                    int(row["attempts"] >= 8),
                    row["id"],
                )
                for row in rows
            ]
            changed = self.db.executemany(
                """
                UPDATE events SET status='failed',last_error=?,next_attempt=?,manual_retry=?
                WHERE id=? AND status='sending'
                """,
                updates,
            ).rowcount
            if changed != len(ids):
                raise RuntimeError("通知队列状态已改变，未能完整记录发送失败")
        return changed

    def mark_unknown_many(self, event_ids: list[int], error: str) -> int:
        ids = self._event_ids(event_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.db:
            changed = self.db.execute(
                f"""
                UPDATE events SET status='failed',last_error=?,next_attempt=0,manual_retry=1
                WHERE id IN ({placeholders}) AND status='sending'
                """,
                [error[:500], *ids],
            ).rowcount
            if changed != len(ids):
                raise RuntimeError("通知队列状态已改变，未能隔离送达状态未知的批次")
        return changed

    def mark_sending(self, event_id: int) -> None:
        self.mark_sending_many([event_id])

    def mark_sent(self, event_id: int) -> None:
        self.mark_sent_many([event_id])

    def mark_failed(self, event_id: int, error: str) -> None:
        self.mark_failed_many([event_id], error)

    def retry(self, owner: str, target: str, event_id: int | None = None) -> int:
        sql = "UPDATE events SET status='pending',manual_retry=0,next_attempt=0,attempts=0 WHERE owner=? AND target=? AND status='failed'"
        params: list[Any] = [owner, target]
        if event_id is not None:
            sql += " AND id=?"
            params.append(event_id)
        with self.db:
            return self.db.execute(sql, params).rowcount

    def events(
        self, owner: str, status: str = "all", limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE owner=?"
        params: list[Any] = [owner]
        if status != "all":
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        result = []
        for row in self.db.execute(sql, params):
            item = dict(row)
            item.pop("event_key")
            item.pop("owner")
            result.append(item)
        return result

    def counts(self, owner: str) -> dict[str, int]:
        counts = {
            "repositories": len(self.repositories(owner)),
            "pending": 0,
            "sent": 0,
            "failed": 0,
        }
        for row in self.db.execute(
            "SELECT status,COUNT(*) AS n FROM events WHERE owner=? GROUP BY status",
            (owner,),
        ):
            key = "pending" if row["status"] == "sending" else row["status"]
            counts[key] = counts.get(key, 0) + row["n"]
        return counts

    def import_records(
        self, owner: str, target: str, seen: list[str], failed: list[Event]
    ) -> tuple[int, int]:
        imported = queued = 0
        with self.db:
            for key in seen:
                imported += self.db.execute(
                    "INSERT OR IGNORE INTO legacy_seen VALUES(?)", (key,)
                ).rowcount
            for event in failed:
                queued += self._enqueue(owner, target, event, ignore_legacy=True)
        return imported, queued
