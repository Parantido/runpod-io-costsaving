"""SQLite persistence for pods, jobs, logs, and audit events."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pods (
    name TEXT PRIMARY KEY,
    pod_id TEXT,
    status TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    pod TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    command TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_logs (
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    line TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    pod TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    pod TEXT NOT NULL,
    action TEXT NOT NULL,
    cron TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Etc/UTC',
    trigger TEXT NOT NULL DEFAULT 'external',
    enabled INTEGER NOT NULL DEFAULT 0,
    pre_hooks TEXT NOT NULL DEFAULT '[]',
    post_hooks TEXT NOT NULL DEFAULT '[]',
    last_job_id TEXT,
    last_run_at TEXT,
    last_status TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_pod ON jobs(pod);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_schedules_pod ON schedules(pod);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    def upsert_pod(self, name: str, pod_id: str | None, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pods(name, pod_id, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    pod_id=excluded.pod_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (name, pod_id, status, _now()),
            )

    def get_pod(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pods WHERE name=?", (name,)
            ).fetchone()
        return dict(row) if row else None

    def list_pods(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM pods ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def create_job(self, pod: str, action: str, command: str) -> str:
        job_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(id, pod, action, status, created_at, command)
                VALUES (?, ?, ?, 'queued', ?, ?)
                """,
                (job_id, pod, action, _now(), command),
            )
        return job_id

    def start_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?",
                (_now(), job_id),
            )

    def finish_job(self, job_id: str, status: str, exit_code: int | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, exit_code=?, finished_at=? WHERE id=?",
                (status, exit_code, _now(), job_id),
            )

    def add_log(self, job_id: str, line: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM job_logs WHERE job_id=?", (job_id,)
            ).fetchone()
            seq = int(row[0]) + 1
            connection.execute(
                "INSERT INTO job_logs(job_id, seq, ts, line) VALUES (?, ?, ?, ?)",
                (job_id, seq, _now(), line),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, pod: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if pod:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE pod=? ORDER BY created_at DESC LIMIT ?",
                    (pod, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def get_logs(self, job_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_logs WHERE job_id=? AND seq>? ORDER BY seq",
                (job_id, after_seq),
            ).fetchall()
        return [dict(row) for row in rows]

    def audit(
        self, actor: str, action: str, pod: str | None, detail: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit(ts, actor, action, pod, detail) VALUES (?, ?, ?, ?, ?)",
                (_now(), actor, action, pod, detail),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _hooks_json(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(list(value or []))

    @staticmethod
    def _schedule_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        for key in ("pre_hooks", "post_hooks"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except (TypeError, ValueError):
                item[key] = []
        return item

    def create_schedule(
        self,
        name: str,
        pod: str,
        action: str,
        cron: str,
        timezone_name: str = "Etc/UTC",
        trigger: str = "external",
        enabled: bool = False,
        pre_hooks: Any = (),
        post_hooks: Any = (),
    ) -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO schedules(
                    name, pod, action, cron, timezone, trigger, enabled,
                    pre_hooks, post_hooks, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    pod,
                    action,
                    cron,
                    timezone_name,
                    trigger,
                    1 if enabled else 0,
                    self._hooks_json(pre_hooks),
                    self._hooks_json(post_hooks),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_schedules(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules ORDER BY name"
            ).fetchall()
        return [self._schedule_row(row) for row in rows]

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id=?", (schedule_id,)
            ).fetchone()
        return self._schedule_row(row) if row else None

    def get_schedule_by_name(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE name=?", (name,)
            ).fetchone()
        return self._schedule_row(row) if row else None

    def update_schedule(self, schedule_id: int, **fields: Any) -> None:
        allowed = {
            "name",
            "pod",
            "action",
            "cron",
            "timezone",
            "trigger",
            "enabled",
            "pre_hooks",
            "post_hooks",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        if "pre_hooks" in updates:
            updates["pre_hooks"] = self._hooks_json(updates["pre_hooks"])
        if "post_hooks" in updates:
            updates["post_hooks"] = self._hooks_json(updates["post_hooks"])
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        updates["updated_at"] = _now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = list(updates.values()) + [schedule_id]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE schedules SET {assignments} WHERE id=?", values
            )

    def delete_schedule(self, schedule_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))

    def set_schedule_next_run(self, schedule_id: int, next_run_at: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE schedules SET next_run_at=?, updated_at=? WHERE id=?",
                (next_run_at, _now(), schedule_id),
            )

    def record_schedule_run(
        self,
        schedule_id: int,
        job_id: str | None,
        status: str,
        next_run_at: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE schedules SET
                    last_job_id=?, last_run_at=?, last_status=?, next_run_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (job_id, _now(), status, next_run_at, _now(), schedule_id),
            )
