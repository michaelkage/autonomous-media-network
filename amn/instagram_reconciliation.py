"""Operator-controlled reconciliation for ambiguous Instagram publishes."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .delivery_events import delivery_id_for

__all__ = ["list_instagram_reconciliation", "resolve_instagram_operation"]


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(database), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _ensure_audit_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_operation_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL,
            container_id TEXT NOT NULL,
            resolution TEXT NOT NULL CHECK (resolution IN ('published', 'not_published')),
            previous_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            actor TEXT NOT NULL,
            note TEXT NOT NULL,
            resolved_at REAL NOT NULL
        )
        """
    )


def list_instagram_reconciliation(database: Path, limit: int = 100) -> list[dict[str, Any]]:
    """List unresolved operations without changing provider or local state."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    if not Path(database).is_file():
        return []
    with closing(_connect(database)) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'instagram_publish_operations'"
        ).fetchone()
        if table is None:
            return []
        columns = {row[1] for row in db.execute("PRAGMA table_info(instagram_publish_operations)")}
        output = "output" if "output" in columns else "NULL"
        rows = db.execute(
            "SELECT operation_key, container_id, status, updated_at, "
            + output
            + " AS output FROM instagram_publish_operations "
            "WHERE status = 'publishing' ORDER BY updated_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "operation_key": str(row["operation_key"]),
            "container_id": str(row["container_id"]),
            "status": str(row["status"]),
            "updated_at": float(row["updated_at"]),
            "output": str(row["output"]) if row["output"] else None,
        }
        for row in rows
    ]


def resolve_instagram_operation(
    database: Path,
    *,
    operation_key: str,
    resolution: str,
    actor: str,
    note: str,
) -> dict[str, Any]:
    """Atomically resolve one ambiguous publish and append an immutable audit record.

    'published' marks the operation complete. 'not_published' marks it failed so
    the normal publisher retry path can create a fresh container. This function
    never calls Meta; the operator must verify the outcome independently first.
    """
    if not isinstance(operation_key, str) or not operation_key.strip():
        raise ValueError("operation_key is required")
    if resolution not in {"published", "not_published"}:
        raise ValueError("resolution must be 'published' or 'not_published'")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 120:
        raise ValueError("actor must contain 1 to 120 characters")
    if not isinstance(note, str) or not note.strip() or len(note) > 500:
        raise ValueError("note must contain 1 to 500 characters")
    if re.search(r"(?i)(access[_ -]?token|client[_ -]?secret|bearer\s+\S+)", note):
        raise ValueError("note must not contain credentials or bearer tokens")

    new_status = "complete" if resolution == "published" else "failed"
    with closing(_connect(database)) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'instagram_publish_operations'"
        ).fetchone()
        if table is None:
            raise ValueError("Instagram operation not found")
        _ensure_audit_table(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(instagram_publish_operations)")
            }
            output_column = "output" if "output" in columns else "NULL"
            delivery_id_column = "delivery_id" if "delivery_id" in columns else "NULL"
            operation = db.execute(
                "SELECT container_id, status, "
                + output_column
                + " AS output, "
                + delivery_id_column
                + " AS delivery_id FROM instagram_publish_operations "
                "WHERE operation_key = ?",
                (operation_key.strip(),),
            ).fetchone()
            if operation is None:
                raise ValueError("Instagram operation not found")
            if operation["status"] != "publishing":
                raise ValueError(
                    f"operation is not awaiting reconciliation (status: {operation['status']})"
                )
            changed = db.execute(
                "UPDATE instagram_publish_operations SET status = ?, updated_at = ? "
                "WHERE operation_key = ? AND status = 'publishing'",
                (new_status, time.time(), operation_key.strip()),
            ).rowcount
            if changed != 1:
                raise ValueError("Instagram operation changed during reconciliation")
            resolved_at = time.time()
            db.execute(
                """
                INSERT INTO instagram_operation_resolutions (
                    operation_key, container_id, resolution, previous_status,
                    new_status, actor, note, resolved_at
                ) VALUES (?, ?, ?, 'publishing', ?, ?, ?, ?)
                """,
                (
                    operation_key.strip(),
                    str(operation["container_id"]),
                    resolution,
                    new_status,
                    actor.strip(),
                    note.strip(),
                    resolved_at,
                ),
            )
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
            db.execute(
                """
                INSERT INTO delivery_events (
                    delivery_id, platform, output, event_type, attempt,
                    will_retry, retry_after_seconds, created_at
                ) VALUES (?, 'instagram', ?, ?, 1, ?, NULL, ?)
                """,
                (
                    operation["delivery_id"]
                    or delivery_id_for("instagram", operation_key.strip()),
                    str(operation["output"])
                    if operation["output"]
                    else "container:" + str(operation["container_id"]),
                    "manual_reconciliation_published"
                    if resolution == "published"
                    else "manual_reconciliation_not_published",
                    int(resolution == "not_published"),
                    resolved_at,
                ),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return {
        "operation_key": operation_key.strip(),
        "container_id": str(operation["container_id"]),
        "resolution": resolution,
        "status": new_status,
        "actor": actor.strip(),
    }


def instagram_resolution_history(database: Path, limit: int = 25) -> list[dict[str, Any]]:
    """Return a bounded, credential-screened audit history for the local dashboard."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if not Path(database).is_file():
        return []
    with closing(_connect(database)) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'instagram_operation_resolutions'"
        ).fetchone()
        if table is None:
            return []
        rows = db.execute(
            "SELECT operation_key, container_id, resolution, previous_status, "
            "new_status, actor, note, resolved_at FROM instagram_operation_resolutions "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "operation_key": str(row["operation_key"]),
            "container_id": str(row["container_id"]),
            "resolution": str(row["resolution"]),
            "previous_status": str(row["previous_status"]),
            "new_status": str(row["new_status"]),
            "actor": str(row["actor"]),
            "note": str(row["note"]),
            "resolved_at": float(row["resolved_at"]),
        }
        for row in rows
    ]
