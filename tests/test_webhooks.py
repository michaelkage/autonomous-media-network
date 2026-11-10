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

    unavailable_body = _payload("post.publish.no_longer_publicaly_available", "publish-123")
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

    inbox_body = _payload("post.publish.inbox_delivered", "publish-123")
    inbox_response = client.post(
        "/webhooks/tiktok",
        content=inbox_body,
        headers=_signed_headers(inbox_body, "test-secret"),
    )
    assert inbox_response.status_code == 200
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
        "provider_webhook_inbox_delivered": 1,
    }
    assert events["recent"][0]["output"] == "publish_id:publish-123"
    assert events["webhooks"]["received"] == 3
    assert events["webhooks"]["last_received_at"] is not None
    assert events["webhooks"]["latest_latency_seconds"] is not None
    assert events["webhooks"]["latest_latency_seconds"] >= 0
    assert events["tiktok_operations"]["pending"] == 0
    assert events["tiktok_operations"]["stalled"] == 0
    assert events["tiktok_operations"]["complete"] == 1
    assert events["tiktok_operations"]["failed"] == 0


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
    missing_id = json.dumps({"event": "post.publish.complete", "content": "{}"}).encode()
    response = configured.post(
        "/webhooks/tiktok",
        content=missing_id,
        headers=_signed_headers(missing_id, "test-secret"),
    )
    assert response.status_code == 400


def test_tiktok_webhook_correlates_to_manifest_delivery(tmp_path: Path) -> None:
    import hashlib

    from amn.delivery_events import delivery_id_for
    from amn.publish import publish_manifest

    (tmp_path / "outbox").mkdir()
    asset = tmp_path / "outbox" / "correlated.jpg"
    asset.write_bytes(b"image-bytes")
    manifest = tmp_path / "build" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps({"passed": [{"output": "outbox/correlated.jpg", "caption": "private caption"}]}),
        encoding="utf-8",
    )
    database = tmp_path / "build" / "jobs.sqlite3"
    publisher = TikTokPublisher(
        "test-access-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        max_polls=0,
        rate_limit_db=database,
    )

    def fake_request(endpoint: str, payload: dict) -> dict:
        if endpoint == "post/publish/creator_info/query":
            return {"data": {"privacy_level_options": ["SELF_ONLY"]}}
        if endpoint == "post/publish/content/init":
            return {"data": {"publish_id": "publish-correlated"}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    publisher._request = fake_request
    assert publish_manifest(manifest, publisher, root=tmp_path, platform="tiktok", retries=0) == 0

    body = _payload("post.publish.complete", "publish-correlated")
    client = TestClient(
        create_webhook_app(tmp_path, client_secret="test-secret"),
        base_url="http://127.0.0.1",
    )
    response = client.post(
        "/webhooks/tiktok",
        content=body,
        headers=_signed_headers(body, "test-secret"),
    )
    assert response.status_code == 200

    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    expected_id = delivery_id_for("tiktok", f"{digest}:outbox/correlated.jpg")
    events = delivery_event_snapshot(database)["recent"]
    callback = next(event for event in events if event["event_type"] == "provider_webhook_complete")
    assert callback["delivery_id"] == expected_id
    assert callback["output"] == "outbox/correlated.jpg"
    assert any(
        event["delivery_id"] == expected_id and event["event_type"] == "attempt_started"
        for event in events
    )


def test_tiktok_operation_table_migrates_legacy_schema(tmp_path: Path) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE tiktok_publish_operations ("
            "operation_key TEXT PRIMARY KEY, publish_id TEXT NOT NULL, "
            "status TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO tiktok_publish_operations VALUES (?, ?, ?, ?)",
            ("legacy-operation", "legacy-publish", "pending", time.time()),
        )

    publisher = TikTokPublisher(
        "test-access-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        rate_limit_db=database,
    )
    assert publisher._load_operation("legacy-operation") == ("legacy-publish", "pending")
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(tiktok_publish_operations)")}
    assert {"delivery_id", "output"} <= columns


def test_webhook_receipt_table_migrates_legacy_schema(tmp_path: Path) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE provider_webhook_receipts ("
            "receipt_key TEXT PRIMARY KEY, provider TEXT NOT NULL, "
            "event_name TEXT NOT NULL, publish_id TEXT, received_at REAL NOT NULL)"
        )

    client = TestClient(
        create_webhook_app(tmp_path, client_secret="test-secret"),
        base_url="http://127.0.0.1",
    )
    body = _payload("post.publish.complete", "legacy-receipt-publish")
    response = client.post(
        "/webhooks/tiktok",
        content=body,
        headers=_signed_headers(body, "test-secret"),
    )
    assert response.status_code == 200
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(provider_webhook_receipts)")}
        event_time = db.execute(
            "SELECT event_created_at FROM provider_webhook_receipts"
        ).fetchone()[0]
    assert "event_created_at" in columns
    assert event_time is not None



def test_tiktok_pending_operation_becomes_stalled_only_in_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    publisher = TikTokPublisher(
        "test-access-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        rate_limit_db=database,
    )
    publisher._save_operation("fresh-operation", "publish-fresh", "pending")
    publisher._save_operation("old-operation", "publish-old", "pending")
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE tiktok_publish_operations SET updated_at = ? WHERE publish_id = ?",
            (time.time() - 3600, "publish-old"),
        )

    snapshot = delivery_event_snapshot(database, stale_after_seconds=900)
    operations = snapshot["tiktok_operations"]
    assert operations["pending"] == 1
    assert operations["stalled"] == 1
    assert operations["stale_after_seconds"] == 900
    assert operations["recent_stalled"][0]["publish_id"] == "publish-old"
    assert operations["recent_stalled"][0]["age_seconds"] >= 3600

    with sqlite3.connect(database) as db:
        statuses = dict(db.execute("SELECT publish_id, status FROM tiktok_publish_operations"))
    assert statuses == {"publish-fresh": "pending", "publish-old": "pending"}


def test_tiktok_stalled_threshold_is_validated(tmp_path: Path) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    try:
        delivery_event_snapshot(database, stale_after_seconds=30)
    except ValueError as exc:
        assert "between 60 and 86400" in str(exc)
    else:
        raise AssertionError("invalid stale threshold should fail")
