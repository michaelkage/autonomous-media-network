"""Durable, deduplicated Telegram operator alerts for AMN incidents."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import requests

__all__ = ["check_operator_alerts", "operator_alert_snapshot"]

_DEFAULT_STALE_SECONDS = 900
_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
_RETRY_SECONDS = 5 * 60


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(database), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _incidents(
    database: Path, *, now: float, stale_after_seconds: int
) -> dict[str, dict[str, str]]:
    incidents: dict[str, dict[str, str]] = {}
    if not Path(database).is_file():
        return incidents

    with closing(_connect(database)) as db:
        if _table_exists(db, "jobs"):
            for row in db.execute(
                "SELECT id, kind, attempts, max_attempts FROM jobs "
                "WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 50"
            ):
                fingerprint = f"job-failed:{row['id']}"
                incidents[fingerprint] = {
                    "kind": "job_failed",
                    "message": (
                        f"AMN job permanently failed: {row['kind']} "
                        f"(job {row['id'][:8]}, attempts {row['attempts']}/{row['max_attempts']})."
                    ),
                }

        if _table_exists(db, "jobs"):
            job_columns = {
                str(row["name"]) for row in db.execute("PRAGMA table_info(jobs)")
            }
            if {"available_at", "lease_until", "updated_at"}.issubset(job_columns):
                for row in db.execute(
                    """
                    SELECT id, kind, status, updated_at, lease_until
                    FROM jobs
                    WHERE (
                        status = 'running'
                        AND lease_until IS NOT NULL
                        AND lease_until <= ?
                    ) OR (
                        status = 'pending'
                        AND available_at <= ?
                        AND updated_at <= ?
                    )
                    ORDER BY updated_at ASC
                    LIMIT 100
                    """,
                    (now, now, now - stale_after_seconds),
                ):
                    job_id = str(row["id"])
                    kind = str(row["kind"])
                    if row["status"] == "running":
                        expired_for = max(0, int(now - float(row["lease_until"])))
                        incidents[f"job-lease-expired:{job_id}"] = {
                            "kind": "job_lease_expired",
                            "message": (
                                f"AMN worker lease expired for {kind} job {job_id[:8]} "
                                f"({expired_for // 60} minutes ago). Check worker health and "
                                "confirm the job is reclaimed."
                            ),
                        }
                    else:
                        age = max(0, int(now - float(row["updated_at"])))
                        incidents[f"job-queue-stalled:{job_id}"] = {
                            "kind": "job_queue_stalled",
                            "message": (
                                f"AMN {kind} job {job_id[:8]} has been ready for "
                                f"{age // 60} minutes without being claimed. Check worker "
                                "availability and queue throughput."
                            ),
                        }

        if _table_exists(db, "tiktok_publish_operations"):
            for row in db.execute(
                "SELECT publish_id, updated_at FROM tiktok_publish_operations "
                "WHERE status = 'pending'"
            ):
                age = max(0, int(now - float(row["updated_at"])))
                if age >= stale_after_seconds:
                    key = str(row["publish_id"])
                    incidents[f"tiktok-stalled:{key}"] = {
                        "kind": "tiktok_stalled",
                        "message": (
                            f"TikTok publish operation is stalled for {age // 60} minutes "
                            f"(publish ID {key[:18]}). Review AMN delivery diagnostics."
                        ),
                    }

        if _table_exists(db, "instagram_publish_operations"):
            for row in db.execute(
                "SELECT operation_key, container_id, status, updated_at "
                "FROM instagram_publish_operations "
                "WHERE status IN ('processing', 'ready', 'publishing')"
            ):
                key = str(row["operation_key"])
                status = str(row["status"])
                age = max(0, int(now - float(row["updated_at"])))
                if status == "publishing":
                    incidents[f"instagram-reconcile:{key}"] = {
                        "kind": "instagram_reconciliation",
                        "message": (
                            "Instagram publication needs manual reconciliation "
                            f"(operation {key[:18]}, container {str(row['container_id'])[:18]}). "
                            "Do not blindly retry this post."
                        ),
                    }
                elif age >= stale_after_seconds:
                    incidents[f"instagram-stalled:{key}"] = {
                        "kind": "instagram_stalled",
                        "message": (
                            f"Instagram container is stalled for {age // 60} minutes "
                            f"(operation {key[:18]}). Review AMN delivery diagnostics."
                        ),
                    }

        if _table_exists(db, "delivery_events"):
            rows = db.execute(
                """
                SELECT delivery_id, platform, output,
                       SUM(CASE WHEN event_type = 'attempt_failed' THEN 1 ELSE 0 END) AS failures,
                       MAX(created_at) AS last_event_at,
                       MAX(CASE WHEN event_type IN (
                           'published', 'manual_reconciliation_published'
                       ) THEN created_at ELSE 0 END) AS last_success_at
                FROM delivery_events
                WHERE created_at >= ?
                GROUP BY delivery_id, platform, output
                HAVING failures >= 3 AND last_event_at > last_success_at
                """,
                (now - 1800,),
            ).fetchall()
            for row in rows:
                key = str(row["delivery_id"])
                incidents[f"delivery-repeated-failure:{key}"] = {
                    "kind": "delivery_repeated_failure",
                    "message": (
                        f"{row['platform']} delivery has failed at least three times in 30 minutes "
                        f"(asset {str(row['output'])[:80]}). Review the delivery timeline."
                    ),
                }

    return incidents


def _send_telegram(bot_token: str, chat_id: str, message: str, timeout: float) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram rejected the operator alert")


def check_operator_alerts(
    database: Path,
    *,
    bot_token: str | None,
    chat_id: str | None,
    stale_after_seconds: int = _DEFAULT_STALE_SECONDS,
    cooldown_seconds: int = _ALERT_COOLDOWN_SECONDS,
    timeout: float = 10.0,
    now: float | None = None,
) -> dict[str, int]:
    """Detect incidents, persist dedupe state, and best-effort notify Telegram."""
    if isinstance(stale_after_seconds, bool) or not 60 <= stale_after_seconds <= 86400:
        raise ValueError("stale_after_seconds must be between 60 and 86400")
    if isinstance(cooldown_seconds, bool) or cooldown_seconds < 60:
        raise ValueError("cooldown_seconds must be at least 60")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    current = time.time() if now is None else float(now)
    incidents = _incidents(database, now=current, stale_after_seconds=stale_after_seconds)
    can_notify = bool(bot_token and chat_id)
    opened = resolved = sent = failed = suppressed = 0
    due_notifications: list[tuple[str, str]] = []

    with closing(_connect(database)) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_alerts (
                fingerprint TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'resolved')),
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                last_attempt_at REAL,
                last_notified_at REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """
        )
        db.execute("BEGIN IMMEDIATE")
        try:
            active_keys = set(incidents)
            existing = db.execute(
                "SELECT fingerprint FROM operator_alerts WHERE status = 'active'"
            ).fetchall()
            for row in existing:
                fingerprint = str(row["fingerprint"])
                if fingerprint not in active_keys:
                    db.execute(
                        "UPDATE operator_alerts SET status = 'resolved', last_seen = ? "
                        "WHERE fingerprint = ? AND status = 'active'",
                        (current, fingerprint),
                    )
                    resolved += 1

            for fingerprint, incident in incidents.items():
                row = db.execute(
                    "SELECT * FROM operator_alerts WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                if row is None:
                    db.execute(
                        """
                        INSERT INTO operator_alerts (
                            fingerprint, kind, message, status, first_seen, last_seen
                        ) VALUES (?, ?, ?, 'active', ?, ?)
                        """,
                        (fingerprint, incident["kind"], incident["message"], current, current),
                    )
                    opened += 1
                    row = db.execute(
                        "SELECT * FROM operator_alerts WHERE fingerprint = ?", (fingerprint,)
                    ).fetchone()
                else:
                    reopened = row["status"] == "resolved"
                    db.execute(
                        "UPDATE operator_alerts SET kind = ?, message = ?, status = 'active', "
                        "last_seen = ?, "
                        "last_attempt_at = CASE WHEN ? THEN NULL ELSE last_attempt_at END, "
                        "last_notified_at = CASE WHEN ? THEN NULL ELSE last_notified_at END, "
                        "last_error = CASE WHEN ? THEN NULL ELSE last_error END "
                        "WHERE fingerprint = ?",
                        (
                            incident["kind"],
                            incident["message"],
                            current,
                            int(reopened),
                            int(reopened),
                            int(reopened),
                            fingerprint,
                        ),
                    )

                if not can_notify:
                    suppressed += 1
                    continue
                last_attempt = row["last_attempt_at"]
                last_notified = row["last_notified_at"]
                if last_attempt is not None and current - float(last_attempt) < _RETRY_SECONDS:
                    suppressed += 1
                    continue
                if last_notified is not None and current - float(last_notified) < cooldown_seconds:
                    suppressed += 1
                    continue
                db.execute(
                    "UPDATE operator_alerts SET last_attempt_at = ?, "
                    "attempt_count = attempt_count + 1 "
                    "WHERE fingerprint = ?",
                    (current, fingerprint),
                )
                due_notifications.append((fingerprint, incident["message"]))
            db.commit()
        except BaseException:
            db.rollback()
            raise

    for fingerprint, message in due_notifications:
        try:
            _send_telegram(
                str(bot_token),
                str(chat_id),
                f"⚠️ AMN ALERT\n{message}",
                timeout,
            )
        except (requests.RequestException, ValueError, RuntimeError, OSError) as exc:
            with closing(_connect(database)) as db:
                db.execute(
                    "UPDATE operator_alerts SET last_error = ? "
                    "WHERE fingerprint = ? AND status = 'active' AND last_attempt_at = ?",
                    (f"notification failed ({type(exc).__name__})", fingerprint, current),
                )
            failed += 1
        else:
            with closing(_connect(database)) as db:
                db.execute(
                    "UPDATE operator_alerts SET last_notified_at = ?, last_error = NULL "
                    "WHERE fingerprint = ? AND status = 'active' AND last_attempt_at = ?",
                    (current, fingerprint, current),
                )
            sent += 1

    return {
        "active": len(incidents),
        "opened": opened,
        "resolved": resolved,
        "sent": sent,
        "failed": failed,
        "suppressed": suppressed,
        "notifications_configured": int(can_notify),
    }


def operator_alert_snapshot(database: Path, *, limit: int = 50) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if not Path(database).is_file():
        return {"counts": {"active": 0, "resolved": 0}, "recent": []}
    with closing(_connect(database)) as db:
        if not _table_exists(db, "operator_alerts"):
            return {"counts": {"active": 0, "resolved": 0}, "recent": []}
        counts = {
            str(row["status"]): int(row["count"])
            for row in db.execute(
                "SELECT status, COUNT(*) AS count FROM operator_alerts GROUP BY status"
            )
        }
        recent = [
            {
                "kind": str(row["kind"]),
                "message": str(row["message"]),
                "status": str(row["status"]),
                "first_seen": float(row["first_seen"]),
                "last_seen": float(row["last_seen"]),
                "last_notified_at": row["last_notified_at"],
                "attempt_count": int(row["attempt_count"]),
                "last_error": row["last_error"],
            }
            for row in db.execute(
                "SELECT kind, message, status, first_seen, last_seen, last_notified_at, "
                "attempt_count, last_error FROM operator_alerts "
                "ORDER BY last_seen DESC LIMIT ?",
                (limit,),
            )
        ]
    return {
        "counts": {
            "active": counts.get("active", 0),
            "resolved": counts.get("resolved", 0),
        },
        "recent": recent,
    }
