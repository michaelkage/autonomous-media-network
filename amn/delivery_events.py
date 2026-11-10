"""Append-only delivery event journal backed by the local AMN SQLite database."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

__all__ = ["record_delivery_event", "delivery_event_snapshot"]


def _connect(database: Path) -> sqlite3.Connection:
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def record_delivery_event(
    database: Path,
    *,
    platform: str,
    delivery_key: str,
    output: str,
    event_type: str,
    attempt: int,
    will_retry: bool = False,
    retry_after_seconds: float | None = None,
) -> None:
    """Append one sanitized event. Captions, tokens, and provider payloads are never stored."""
    if not platform or not delivery_key or not output:
        raise ValueError("platform, delivery_key, and output are required")
    if event_type not in {
        "attempt_started",
        "provider_accepted",
        "published",
        "attempt_failed",
        "retry_scheduled",
    }:
        raise ValueError(f"unsupported delivery event type: {event_type}")
    if isinstance(attempt, bool) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if retry_after_seconds is not None and retry_after_seconds < 0:
        raise ValueError("retry_after_seconds cannot be negative")

    delivery_material = f"{platform}\0{delivery_key}"
    delivery_id = hashlib.sha256(delivery_material.encode()).hexdigest()
    with closing(_connect(database)) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                output TEXT NOT NULL,
                event_type TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                will_retry INTEGER NOT NULL DEFAULT 0,
                retry_after_seconds REAL,
                created_at REAL NOT NULL
            )
            """
        )
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """
                INSERT INTO delivery_events (
                    delivery_id, platform, output, event_type, attempt,
                    will_retry, retry_after_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    platform,
                    output,
                    event_type,
                    attempt,
                    int(will_retry),
                    retry_after_seconds,
                    time.time(),
                ),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def delivery_event_snapshot(database: Path, limit: int = 100) -> dict[str, Any]:
    """Return bounded, sanitized event history and aggregate counts."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    if not Path(database).is_file():
        return {"counts": {}, "recent": []}

    with closing(_connect(database)) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'delivery_events'"
        ).fetchone()
        if table is None:
            return {"counts": {}, "recent": []}
        counts = {
            str(row["event_type"]): int(row["count"])
            for row in db.execute(
                "SELECT event_type, COUNT(*) AS count FROM delivery_events GROUP BY event_type"
            )
        }
        recent = [
            {
                "id": int(row["id"]),
                "delivery_id": str(row["delivery_id"]),
                "platform": str(row["platform"]),
                "output": str(row["output"]),
                "event_type": str(row["event_type"]),
                "attempt": int(row["attempt"]),
                "will_retry": bool(row["will_retry"]),
                "retry_after_seconds": row["retry_after_seconds"],
                "created_at": float(row["created_at"]),
            }
            for row in db.execute(
                """
                SELECT id, delivery_id, platform, output, event_type, attempt,
                       will_retry, retry_after_seconds, created_at
                FROM delivery_events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            )
        ]
    return {"counts": counts, "recent": recent}
