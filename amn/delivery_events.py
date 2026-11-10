"""Append-only delivery event journal backed by the local AMN SQLite database."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

__all__ = ["delivery_id_for", "record_delivery_event", "delivery_event_snapshot"]


def _connect(database: Path) -> sqlite3.Connection:
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def delivery_id_for(platform: str, delivery_key: str) -> str:
    """Return a stable delivery identifier for a platform and its delivery key."""
    if not platform or not delivery_key:
        raise ValueError("platform and delivery_key are required")
    material = f"{platform}\0{delivery_key}"
    return hashlib.sha256(material.encode()).hexdigest()


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
        "manual_reconciliation_published",
        "manual_reconciliation_not_published",
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


def delivery_event_snapshot(
    database: Path, limit: int = 100, stale_after_seconds: int = 900
) -> dict[str, Any]:
    """Return bounded delivery history and age-based provider reconciliation."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or not 60 <= stale_after_seconds <= 86400
    ):
        raise ValueError("stale_after_seconds must be an integer between 60 and 86400")
    empty = {
        "counts": {},
        "recent": [],
        "webhooks": {
            "received": 0,
            "last_received_at": None,
            "latest_latency_seconds": None,
        },
        "tiktok_operations": {
            "pending": 0,
            "stalled": 0,
            "complete": 0,
            "failed": 0,
            "stale_after_seconds": stale_after_seconds,
            "recent_stalled": [],
        },
        "instagram_operations": {
            "pending": 0,
            "stalled": 0,
            "complete": 0,
            "failed": 0,
            "reconciliation_required": 0,
            "stale_after_seconds": stale_after_seconds,
            "recent_stalled": [],
            "recent_ambiguous": [],
            "resolution_history": [],
        },
    }
    if not Path(database).is_file():
        return empty

    now = time.time()
    with closing(_connect(database)) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'delivery_events'"
        ).fetchone()
        if table is None:
            counts: dict[str, int] = {}
            recent: list[dict[str, Any]] = []
        else:
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

        webhook_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'provider_webhook_receipts'"
        ).fetchone()
        webhooks = empty["webhooks"].copy()
        if webhook_table:
            columns = {row[1] for row in db.execute("PRAGMA table_info(provider_webhook_receipts)")}
            row = db.execute(
                "SELECT COUNT(*) AS received, MAX(received_at) AS last_received_at "
                "FROM provider_webhook_receipts WHERE provider = 'tiktok'"
            ).fetchone()
            webhooks["received"] = int(row["received"])
            webhooks["last_received_at"] = row["last_received_at"]
            latency = (
                db.execute(
                    "SELECT received_at, event_created_at FROM provider_webhook_receipts "
                    "WHERE provider = 'tiktok' AND event_created_at IS NOT NULL "
                    "ORDER BY received_at DESC LIMIT 1"
                ).fetchone()
                if "event_created_at" in columns
                else None
            )
            if latency and latency["event_created_at"] is not None:
                webhooks["latest_latency_seconds"] = max(
                    0.0, float(latency["received_at"]) - float(latency["event_created_at"])
                )

        operations_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'tiktok_publish_operations'"
        ).fetchone()
        operations = empty["tiktok_operations"].copy()
        if operations_table:
            columns = {row[1] for row in db.execute("PRAGMA table_info(tiktok_publish_operations)")}
            output_column = "output" if "output" in columns else "NULL"
            rows = db.execute(
                "SELECT publish_id, status, updated_at, "
                + output_column
                + " AS output FROM tiktok_publish_operations"
            ).fetchall()
            stalled: list[dict[str, Any]] = []
            for row in rows:
                status = str(row["status"])
                if status == "pending":
                    age = max(0.0, now - float(row["updated_at"]))
                    if age >= stale_after_seconds:
                        operations["stalled"] += 1
                        stalled.append(
                            {
                                "publish_id": str(row["publish_id"]),
                                "output": str(row["output"]) if row["output"] else None,
                                "age_seconds": int(age),
                            }
                        )
                    else:
                        operations["pending"] += 1
                elif status in {"complete", "failed"}:
                    operations[status] += 1
            operations["recent_stalled"] = sorted(
                stalled, key=lambda item: item["age_seconds"], reverse=True
            )[:25]

        instagram_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'instagram_publish_operations'"
        ).fetchone()
        instagram = {
            "pending": 0,
            "stalled": 0,
            "complete": 0,
            "failed": 0,
            "reconciliation_required": 0,
            "stale_after_seconds": stale_after_seconds,
            "recent_stalled": [],
            "recent_ambiguous": [],
            "resolution_history": [],
        }
        if instagram_table:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(instagram_publish_operations)")
            }
            output_column = "output" if "output" in columns else "NULL"
            rows = db.execute(
                "SELECT operation_key, container_id, status, updated_at, "
                + output_column
                + " AS output FROM instagram_publish_operations"
            ).fetchall()
            stalled_rows: list[dict[str, Any]] = []
            ambiguous_rows: list[dict[str, Any]] = []
            for row in rows:
                status = str(row["status"])
                age = max(0.0, now - float(row["updated_at"]))
                item = {
                    "operation_key": str(row["operation_key"]),
                    "container_id": str(row["container_id"]),
                    "output": str(row["output"]) if row["output"] else None,
                    "age_seconds": int(age),
                }
                if status in {"processing", "ready"}:
                    if age >= stale_after_seconds:
                        instagram["stalled"] += 1
                        stalled_rows.append(item)
                    else:
                        instagram["pending"] += 1
                elif status == "publishing":
                    instagram["reconciliation_required"] += 1
                    ambiguous_rows.append(item)
                elif status in {"complete", "failed"}:
                    instagram[status] += 1
            instagram["recent_stalled"] = sorted(
                stalled_rows, key=lambda item: item["age_seconds"], reverse=True
            )[:25]
            instagram["recent_ambiguous"] = sorted(
                ambiguous_rows, key=lambda item: item["age_seconds"], reverse=True
            )[:25]

        resolution_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'instagram_operation_resolutions'"
        ).fetchone()
        if resolution_table:
            instagram["resolution_history"] = [
                {
                    "operation_key": str(row["operation_key"]),
                    "container_id": str(row["container_id"]),
                    "resolution": str(row["resolution"]),
                    "new_status": str(row["new_status"]),
                    "actor": str(row["actor"]),
                    "note": str(row["note"]),
                    "resolved_at": float(row["resolved_at"]),
                }
                for row in db.execute(
                    "SELECT operation_key, container_id, resolution, new_status, "
                    "actor, note, resolved_at FROM instagram_operation_resolutions "
                    "ORDER BY id DESC LIMIT 25"
                )
            ]

    return {
        "counts": counts,
        "recent": recent,
        "webhooks": webhooks,
        "tiktok_operations": operations,
        "instagram_operations": instagram,
    }
