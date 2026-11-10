"""SQLite-backed durable job queue with leases and retry recovery.

The queue uses only the Python standard library. SQLite transactions make claims
safe across processes; leases allow another worker to recover jobs after crashes.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

__all__ = ["JobQueue"]


class JobQueue:
    """A small, process-safe queue suitable for a single-host AMN deployment."""

    def __init__(self, database: Path, *, busy_timeout: float = 10.0) -> None:
        self.database = Path(database).resolve()
        self.busy_timeout = max(0.1, float(busy_timeout))
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=self.busy_timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'running', 'succeeded', 'failed')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
                    available_at REAL NOT NULL,
                    worker_id TEXT,
                    locked_at REAL,
                    lease_until REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    dedupe_key TEXT UNIQUE
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS jobs_ready_idx "
                "ON jobs(status, available_at, created_at)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS jobs_lease_idx ON jobs(status, lease_until)")

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        max_attempts: int = 5,
        delay: float = 0,
        dedupe_key: str | None = None,
    ) -> str:
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 64:
            raise ValueError("job kind must be a non-empty string of at most 64 characters")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("job payload must be an object")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if delay < 0:
            raise ValueError("delay cannot be negative")
        if dedupe_key is not None and (not isinstance(dedupe_key, str) or not dedupe_key.strip()):
            raise ValueError("dedupe_key must be a non-empty string")
        now = time.time()
        job_id = str(uuid.uuid4())
        encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    id, kind, payload, status, attempts, max_attempts,
                    available_at, created_at, updated_at, dedupe_key
                ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)
                """,
                (job_id, kind.strip(), encoded, max_attempts, now + delay, now, now, dedupe_key),
            )
            if cursor.rowcount == 0 and dedupe_key:
                existing = db.execute(
                    "SELECT id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if existing:
                    return str(existing["id"])
        return job_id

    def claim(self, worker_id: str, *, lease_seconds: float = 300) -> dict[str, Any] | None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            # A crashed worker's lease expires. Retry if budget remains, otherwise
            # make the failure terminal instead of leaving an immortal running job.
            db.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                    worker_id = NULL, locked_at = NULL, lease_until = NULL,
                    available_at = ?, updated_at = ?,
                    last_error = COALESCE(last_error, 'worker lease expired')
                WHERE status = 'running' AND lease_until <= ?
                """,
                (now, now, now),
            )
            row = db.execute(
                """
                SELECT * FROM jobs AS candidate
                WHERE candidate.status = 'pending'
                  AND candidate.available_at <= ?
                  AND (
                    candidate.kind != 'pipeline'
                    OR NOT EXISTS (
                        SELECT 1 FROM jobs AS active
                        WHERE active.kind = 'pipeline'
                          AND active.status = 'running'
                          AND active.lease_until > ?
                    )
                  )
                ORDER BY candidate.available_at, candidate.created_at
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            changed = db.execute(
                """
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1,
                    worker_id = ?, locked_at = ?, lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (worker_id, now, now + lease_seconds, now, row["id"]),
            ).rowcount
            if changed != 1:
                db.rollback()
                return None
            claimed = db.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            db.commit()
            return self._decode(claimed)

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: float = 300) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        with closing(self._connect()) as db:
            changed = db.execute(
                """
                UPDATE jobs SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (now + lease_seconds, now, job_id, worker_id),
            ).rowcount
        return changed == 1

    def complete(self, job_id: str, worker_id: str) -> bool:
        now = time.time()
        with closing(self._connect()) as db:
            changed = db.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', worker_id = NULL, locked_at = NULL,
                    lease_until = NULL, last_error = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (now, job_id, worker_id),
            ).rowcount
        return changed == 1

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        retry_delay: float | None = None,
    ) -> bool:
        now = time.time()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT attempts, max_attempts FROM jobs
                WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                db.rollback()
                return False
            terminal = row["attempts"] >= row["max_attempts"]
            delay = (
                min(3600.0, 2 ** min(row["attempts"], 10))
                if retry_delay is None
                else max(0.0, retry_delay)
            )
            changed = db.execute(
                """
                UPDATE jobs
                SET status = ?, available_at = ?, worker_id = NULL,
                    locked_at = NULL, lease_until = NULL, last_error = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (
                    "failed" if terminal else "pending",
                    now if terminal else now + delay,
                    str(error)[:4000],
                    now,
                    job_id,
                    worker_id,
                ),
            ).rowcount
            db.commit()
        return changed == 1

    def get(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode(row) if row else None

    def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status is not None and status not in {"pending", "running", "succeeded", "failed"}:
            raise ValueError("invalid job status")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with closing(self._connect()) as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._decode(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT status, COUNT(*) AS total FROM jobs GROUP BY status")
            counts = {name: 0 for name in ("pending", "running", "succeeded", "failed")}
            counts.update({row["status"]: row["total"] for row in rows})
        return counts
