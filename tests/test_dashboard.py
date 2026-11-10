from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from amn.dashboard import create_app
from amn.jobs import JobQueue


def test_dashboard_serves_ui_and_workspace_status(tmp_path: Path) -> None:
    for folder in ("inbox", "outbox", "rejected", "build"):
        (tmp_path / folder).mkdir()
    (tmp_path / "inbox" / "queued.jpg").write_bytes(b"asset")

    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    page = client.get("/")
    status = client.get("/api/status")

    assert page.status_code == 200
    assert "Autonomous Media Network" in page.text
    assert status.json()["inbox"]["count"] == 1


def test_dashboard_blocks_cross_origin_mutations(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    response = client.put(
        "/api/config",
        headers={"Origin": "https://attacker.example"},
        json={"targets": [], "routes": {}, "thresholds": {"min_likes": 500, "max_distance": 5}},
    )
    assert response.status_code == 403
    assert not (tmp_path / "config" / "amn.json").exists()


def test_dashboard_rejects_non_loopback_host(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), base_url="http://attacker.example")
    assert client.get("/api/status").status_code == 403


def test_dashboard_saves_only_valid_configuration(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    invalid = client.put(
        "/api/config",
        json={
            "targets": [{"id": "telegram", "platform": "telegram", "token": "plaintext-secret"}],
            "routes": {},
            "thresholds": {"min_likes": 500, "max_distance": 5},
        },
    )
    valid = client.put(
        "/api/config",
        json={
            "sources": {"reddit": ["earthporn"]},
            "targets": [],
            "routes": {"default": []},
            "thresholds": {"min_likes": 500, "max_distance": 5},
        },
    )

    assert invalid.status_code == 400
    assert valid.status_code == 200
    saved = json.loads((tmp_path / "config" / "amn.json").read_text(encoding="utf-8"))
    assert saved["sources"]["reddit"] == ["earthporn"]


def test_dashboard_recursively_redacts_secret_values(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "amn.json").write_text(
        json.dumps(
            {
                "sources": {"token": "nested-secret"},
                "targets": [
                    {
                        "id": "telegram",
                        "platform": "telegram",
                        "token": "bot-secret",
                        "token_env": "TELEGRAM_BOT_TOKEN",
                    }
                ],
                "routes": {},
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")

    config = client.get("/api/config").json()

    assert "token" not in config["sources"]
    assert "token" not in config["targets"][0]
    assert config["targets"][0]["token_env"] == "TELEGRAM_BOT_TOKEN"


def test_dashboard_exposes_durable_job_counts_and_recent_jobs(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "build" / "jobs.sqlite3")
    job_id = queue.enqueue("fetch", {"query": "earthporn"})

    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    jobs = client.get("/api/status").json()["jobs"]

    assert jobs["counts"]["pending"] == 1
    assert jobs["recent"][0]["id"] == job_id
    assert jobs["recent"][0]["kind"] == "fetch"
    assert "payload" not in jobs["recent"][0]


def test_dashboard_exposes_schedule_metadata_without_payload(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "build" / "jobs.sqlite3")
    queue.add_schedule(
        "hourly-fetch",
        "fetch",
        {"query": "earthporn", "private_note": "must not leak"},
        3600,
        first_run_at=1000,
    )

    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    schedules = client.get("/api/status").json()["schedules"]

    assert schedules["counts"] == {"enabled": 1, "disabled": 0}
    assert schedules["recent"][0]["id"] == "hourly-fetch"
    assert schedules["recent"][0]["kind"] == "fetch"
    assert "payload" not in schedules["recent"][0]
    assert "private_note" not in json.dumps(schedules)


def test_dashboard_exposes_sanitized_delivery_event_history(tmp_path: Path) -> None:
    from amn.delivery_events import record_delivery_event

    database = tmp_path / "build" / "jobs.sqlite3"
    record_delivery_event(
        database,
        platform="instagram",
        delivery_key="private-digest-key",
        output="outbox/image.jpg",
        event_type="attempt_started",
        attempt=1,
    )
    record_delivery_event(
        database,
        platform="instagram",
        delivery_key="private-digest-key",
        output="outbox/image.jpg",
        event_type="published",
        attempt=1,
    )

    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    response = client.get("/api/deliveries?limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"] == {"attempt_started": 1, "published": 1}
    assert payload["recent"][0]["event_type"] == "published"
    assert payload["recent"][0]["platform"] == "instagram"
    assert "private-digest-key" not in json.dumps(payload)
    assert "caption" not in json.dumps(payload)


def test_dashboard_exposes_instagram_container_and_provider_error_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute(
            """
            CREATE TABLE instagram_publish_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_key TEXT NOT NULL,
                container_id TEXT,
                event_kind TEXT NOT NULL,
                container_status TEXT,
                phase TEXT,
                http_status INTEGER,
                error_type TEXT,
                error_code TEXT,
                error_subcode TEXT,
                fbtrace_id TEXT,
                message TEXT,
                delivery_id TEXT,
                output TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO instagram_publish_history (
                operation_key, container_id, event_kind, container_status,
                phase, http_status, error_type, error_code, error_subcode,
                fbtrace_id, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hashed-operation-key",
                "container-expired",
                "provider_error",
                None,
                "media_publish",
                400,
                "OAuthException",
                "9007",
                "2207052",
                "trace-123",
                "Invalid media URL",
                time.time(),
            ),
        )

    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    response = client.get("/api/deliveries")

    assert response.status_code == 200
    history = response.json()["instagram_operations"]["container_history"]
    assert len(history) == 1
    assert history[0]["container_id"] == "container-expired"
    assert history[0]["error_code"] == "9007"
    assert history[0]["error_subcode"] == "2207052"
    assert history[0]["fbtrace_id"] == "trace-123"
    assert history[0]["message"] == "Invalid media URL"


def test_dashboard_delivery_history_rejects_invalid_limit(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    response = client.get("/api/deliveries?limit=501")
    assert response.status_code == 400


def test_dashboard_delivery_history_rejects_invalid_stale_threshold(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    response = client.get("/api/deliveries?stale_after_seconds=30")
    assert response.status_code == 400


def test_dashboard_exposes_instagram_reconciliation_audit(tmp_path: Path) -> None:
    from amn.instagram_reconciliation import resolve_instagram_operation

    database = tmp_path / "build" / "jobs.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE instagram_publish_operations ("
            "operation_key TEXT PRIMARY KEY, container_id TEXT NOT NULL, "
            "status TEXT NOT NULL, updated_at REAL NOT NULL, delivery_id TEXT, output TEXT)"
        )
        db.execute(
            "INSERT INTO instagram_publish_operations "
            "(operation_key, container_id, status, updated_at, output) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ambiguous-key", "container-123", "publishing", time.time(), "outbox/post.jpg"),
        )
    resolve_instagram_operation(
        database,
        operation_key="ambiguous-key",
        resolution="published",
        actor="dashboard-test",
        note="Verified the post is live.",
    )

    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    response = client.get("/api/deliveries")

    assert response.status_code == 200
    audit = response.json()["instagram_operations"]["resolution_history"]
    assert len(audit) == 1
    assert audit[0]["operation_key"] == "ambiguous-key"
    assert audit[0]["resolution"] == "published"
    assert audit[0]["actor"] == "dashboard-test"
    assert "Verified the post is live" in audit[0]["note"]
    assert "Instagram reconciliation audit" in client.get("/").text
