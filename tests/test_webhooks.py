from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from amn.delivery_events import delivery_event_snapshot
from amn.publishers.tiktok import TikTokPublisher
from amn.webhooks import create_webhook_app


def _signed_headers(body: bytes, secret: str, timestamp: int | None = None) -> dict[str, str]:
    issued_at = int(time.time()) if timestamp is None else timestamp
    signed = str(issued_at).encode() + b"." + body
    signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return {"TikTok-Signature": f"t={issued_at},s={signature}"}


def _payload(event: str, publish_id: str) -> bytes:
    return json.dumps(
        {
            "client_key": "test-client",
            "event": event,
            "create_time": int(time.time()),
            "user_openid": "user-open-id",
            "content": json.dumps({"publish_id": publish_id}),
        },
        separators=(",", ":"),
    ).encode()


def test_tiktok_webhook_verifies_signature_and_deduplicates(tmp_path: Path) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    publisher = TikTokPublisher(
        "test-access-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        rate_limit_db=database,
    )
    publisher._save_operation("operation-key", "publish-123", "pending")

    client = TestClient(
        create_webhook_app(tmp_path, client_secret="test-secret", client_key="test-client"),
        base_url="http://127.0.0.1",
    )
    body = _payload("post.publish.complete", "publish-123")
    headers = _signed_headers(body, "test-secret")

    first = client.post("/webhooks/tiktok", content=body, headers=headers)
    duplicate = client.post("/webhooks/tiktok", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json() == {"status": "accepted"}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}

    with sqlite3.connect(database) as db:
        status = db.execute(
            "SELECT status FROM tiktok_publish_operations WHERE publish_id = ?",
            ("publish-123",),
        ).fetchone()[0]
        receipt_count = db.execute("SELECT COUNT(*) FROM provider_webhook_receipts").fetchone()[0]
    assert status == "complete"
    assert receipt_count == 1

    unavailable_body = _payload(
        "post.publish.no_longer_publicaly_available", "publish-123"
    )
    unavailable = client.post(
        "/webhooks/tiktok",
        content=unavailable_body,
        headers=_signed_headers(unavailable_body, "test-secret"),
    )
    assert unavailable.status_code == 200
    with sqlite3.connect(database) as db:
        status = db.execute(
            "SELECT status FROM tiktok_publish_operations WHERE publish_id = ?",
            ("publish-123",),
        ).fetchone()[0]
    assert status == "complete"

    events = delivery_event_snapshot(database)
    assert events["counts"] == {
        "provider_webhook_complete": 1,
        "provider_webhook_unavailable": 1,
    }
    assert events["recent"][0]["output"] == "publish_id:publish-123"


def test_tiktok_failed_webhook_marks_operation_retryable(tmp_path: Path) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    publisher = TikTokPublisher(
        "test-access-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        rate_limit_db=database,
    )
    publisher._save_operation("operation-key", "publish-456", "pending")
    client = TestClient(
        create_webhook_app(tmp_path, client_secret="test-secret"),
        base_url="http://127.0.0.1",
    )
    body = _payload("post.publish.failed", "publish-456")

    response = client.post(
        "/webhooks/tiktok",
        content=body,
        headers=_signed_headers(body, "test-secret"),
    )

    assert response.status_code == 200
    with sqlite3.connect(database) as db:
        status = db.execute(
            "SELECT status FROM tiktok_publish_operations WHERE publish_id = ?",
            ("publish-456",),
        ).fetchone()[0]
    assert status == "failed"


def test_tiktok_webhook_rejects_bad_signature_stale_timestamp_and_wrong_client(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_webhook_app(
            tmp_path,
            client_secret="test-secret",
            client_key="expected-client",
            tolerance_seconds=60,
        ),
        base_url="http://127.0.0.1",
    )
    body = _payload("post.publish.complete", "publish-789")

    bad_signature = client.post(
        "/webhooks/tiktok",
        content=body,
        headers={"TikTok-Signature": "t=1,s=invalid"},
    )
    stale_timestamp = client.post(
        "/webhooks/tiktok",
        content=body,
        headers=_signed_headers(body, "test-secret", int(time.time()) - 3600),
    )
    wrong_client_body = body.replace(b"expected-client", b"wrong-client")
    wrong_client = client.post(
        "/webhooks/tiktok",
        content=wrong_client_body,
        headers=_signed_headers(wrong_client_body, "test-secret"),
    )

    assert bad_signature.status_code == 401
    assert stale_timestamp.status_code == 401
    assert wrong_client.status_code == 401


def test_tiktok_webhook_requires_secret_and_publish_id(tmp_path: Path) -> None:
    unconfigured = TestClient(
        create_webhook_app(tmp_path, client_secret=""),
        base_url="http://127.0.0.1",
    )
    body = _payload("post.publish.complete", "publish-000")
    assert unconfigured.post("/webhooks/tiktok", content=body).status_code == 503

    configured = TestClient(
        create_webhook_app(tmp_path, client_secret="test-secret"),
        base_url="http://127.0.0.1",
    )
    missing_id = json.dumps(
        {"event": "post.publish.complete", "content": "{}"}
    ).encode()
    response = configured.post(
        "/webhooks/tiktok",
        content=missing_id,
        headers=_signed_headers(missing_id, "test-secret"),
    )
    assert response.status_code == 400
