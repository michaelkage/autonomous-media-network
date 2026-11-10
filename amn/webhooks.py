"""Public provider webhook receiver, separate from the loopback diagnostics UI."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .delivery_events import delivery_id_for

_TIKTOK_EVENTS = {
    "post.publish.failed": "provider_webhook_failed",
    "post.publish.complete": "provider_webhook_complete",
    "post.publish.publicly_available": "provider_webhook_publicly_available",
    "post.publish.no_longer_publicaly_available": "provider_webhook_unavailable",
    "post.publish.inbox_delivered": "provider_webhook_inbox_delivered",
}


def _verify_tiktok_signature(
    raw_body: bytes, header: str, client_secret: str, *, now: float, tolerance: int
) -> bool:
    values: dict[str, str] = {}
    for part in header.split(","):
        key, separator, value = part.strip().partition("=")
        if not separator or key in values:
            return False
        values[key] = value
    timestamp, signature = values.get("t"), values.get("s")
    if not timestamp or not signature:
        return False
    try:
        issued_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - issued_at) > tolerance:
        return False
    signed_payload = timestamp.encode() + b"." + raw_body
    expected = hmac.new(client_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _record_tiktok_webhook(
    database: Path,
    *,
    receipt_key: str,
    event_name: str,
    publish_id: str | None,
    event_type: str | None,
    created_at: float,
) -> bool:
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=30)) as db:
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_webhook_receipts (
                receipt_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                event_name TEXT NOT NULL,
                publish_id TEXT,
                received_at REAL NOT NULL
            )
            """
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
        db.execute("BEGIN IMMEDIATE")
        try:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO provider_webhook_receipts
                    (receipt_key, provider, event_name, publish_id, received_at)
                VALUES (?, 'tiktok', ?, ?, ?)
                """,
                (receipt_key, event_name, publish_id, time.time()),
            )
            if cursor.rowcount == 0:
                db.commit()
                return False

            if publish_id and event_type:
                operation_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'tiktok_publish_operations'"
                ).fetchone()
                operation = (
                    db.execute(
                        "SELECT operation_key FROM tiktok_publish_operations WHERE publish_id = ?",
                        (publish_id,),
                    ).fetchone()
                    if operation_table
                    else None
                )
                if operation:
                    status = (
                        "failed"
                        if event_type == "provider_webhook_failed"
                        else "complete"
                        if event_type
                        in {"provider_webhook_complete", "provider_webhook_publicly_available"}
                        else "pending"
                    )
                    db.execute(
                        "UPDATE tiktok_publish_operations SET status = ?, updated_at = ? "
                        "WHERE publish_id = ?",
                        (status, time.time(), publish_id),
                    )
                db.execute(
                    """
                    INSERT INTO delivery_events (
                        delivery_id, platform, output, event_type, attempt,
                        will_retry, retry_after_seconds, created_at
                    ) VALUES (?, 'tiktok', ?, ?, 1, 0, NULL, ?)
                    """,
                    (
                        delivery_id_for("tiktok-webhook", publish_id),
                        f"publish_id:{publish_id}",
                        event_type,
                        created_at,
                    ),
                )
            db.commit()
            return True
        except BaseException:
            db.rollback()
            raise


def create_webhook_app(
    root: Path,
    *,
    client_secret: str | None = None,
    client_key: str | None = None,
    tolerance_seconds: int = 300,
) -> Any:
    """Create the public webhook ASGI app; deploy behind an HTTPS reverse proxy."""
    try:
        from fastapi import FastAPI, HTTPException, Request
    except ModuleNotFoundError as exc:
        raise RuntimeError('Install webhook dependencies: pip install -e ".[dashboard]"') from exc

    if isinstance(tolerance_seconds, bool) or tolerance_seconds < 1:
        raise ValueError("tolerance_seconds must be positive")
    root = Path(root).resolve()
    secret = client_secret or os.environ.get("TIKTOK_CLIENT_SECRET", "")
    expected_client_key = client_key or os.environ.get("TIKTOK_CLIENT_KEY")
    app = FastAPI(title="AMN Provider Webhooks", docs_url=None, redoc_url=None)

    @app.post("/webhooks/tiktok")
    async def tiktok_webhook(request: Request) -> dict[str, str]:
        if not secret:
            raise HTTPException(status_code=503, detail="TikTok webhook secret is not configured")
        raw_body = await request.body()
        signature = request.headers.get("TikTok-Signature", "")
        if not _verify_tiktok_signature(
            raw_body, signature, secret, now=time.time(), tolerance=tolerance_seconds
        ):
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
            raise HTTPException(status_code=400, detail="invalid TikTok webhook envelope")
        if expected_client_key and not hmac.compare_digest(
            str(payload.get("client_key", "")), expected_client_key
        ):
            raise HTTPException(status_code=401, detail="unexpected TikTok client key")

        event_name = payload["event"]
        event_type = _TIKTOK_EVENTS.get(event_name)
        publish_id: str | None = None
        if event_type:
            content = payload.get("content", "{}")
            try:
                content_data = json.loads(content) if isinstance(content, str) else content
            except (TypeError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=400, detail="invalid event content") from exc
            if not isinstance(content_data, dict):
                raise HTTPException(status_code=400, detail="invalid event content")
            publish_id = content_data.get("publish_id")
            if not isinstance(publish_id, str) or not publish_id:
                raise HTTPException(status_code=400, detail="publish_id is required")

        receipt_key = hashlib.sha256(raw_body).hexdigest()
        try:
            inserted = _record_tiktok_webhook(
                root / "build" / "jobs.sqlite3",
                receipt_key=receipt_key,
                event_name=event_name,
                publish_id=publish_id,
                event_type=event_type,
                created_at=float(payload.get("create_time") or time.time()),
            )
        except sqlite3.Error as exc:
            raise HTTPException(
                status_code=503, detail="webhook event could not be persisted"
            ) from exc
        return {"status": "accepted" if inserted else "duplicate"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AMN provider webhook receiver")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise SystemExit('Install webhook dependencies: pip install -e ".[dashboard]"') from exc
    uvicorn.run(create_webhook_app(args.root), host=args.host, port=args.port)
