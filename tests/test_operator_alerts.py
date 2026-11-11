from __future__ import annotations

import sqlite3
from pathlib import Path

from amn.delivery_events import record_delivery_event
from amn.jobs import JobQueue
from amn.operator_alerts import check_operator_alerts, operator_alert_snapshot
from amn.pipeline import main


def test_failed_job_alert_is_sent_once_then_resolved(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    queue = JobQueue(database)
    job_id = queue.enqueue("publish", max_attempts=1)
    assert queue.claim("worker") is not None
    assert queue.fail(job_id, "worker", "provider failure", retry_delay=0)

    calls: list[tuple[str, str]] = []

    def fake_send(token: str, chat_id: str, message: str, timeout: float) -> None:
        calls.append((chat_id, message))

    monkeypatch.setattr("amn.operator_alerts._send_telegram", fake_send)

    first = check_operator_alerts(database, bot_token="test-token", chat_id="@ops", now=1000)
    second = check_operator_alerts(database, bot_token="test-token", chat_id="@ops", now=1100)

    assert first["active"] == 1
    assert first["sent"] == 1
    assert second["sent"] == 0
    assert len(calls) == 1
    assert "provider failure" not in calls[0][1]
    assert operator_alert_snapshot(database)["counts"]["active"] == 1

    with sqlite3.connect(database) as db:
        db.execute("UPDATE jobs SET status = 'succeeded' WHERE id = ?", (job_id,))

    resolved = check_operator_alerts(database, bot_token="test-token", chat_id="@ops", now=1200)
    assert resolved["active"] == 0
    assert resolved["resolved"] == 1
    assert operator_alert_snapshot(database)["counts"]["resolved"] == 1


def test_repeated_delivery_failures_create_one_alert(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    for attempt in range(1, 4):
        record_delivery_event(
            database,
            platform="telegram",
            delivery_key="private-delivery-key",
            output="outbox/image.jpg",
            event_type="attempt_failed",
            attempt=attempt,
        )
    calls: list[str] = []
    monkeypatch.setattr(
        "amn.operator_alerts._send_telegram",
        lambda token, chat, message, timeout: calls.append(message),
    )

    result = check_operator_alerts(database, bot_token="token", chat_id="@ops", now=2000)

    assert result["active"] == 1
    assert result["sent"] == 1
    assert len(calls) == 1
    assert "telegram" in calls[0]
    assert "private-delivery-key" not in calls[0]


def test_stalled_provider_operations_are_alerted(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE tiktok_publish_operations "
            "(publish_id TEXT PRIMARY KEY, status TEXT, updated_at REAL)"
        )
        db.execute(
            "INSERT INTO tiktok_publish_operations VALUES ('publish-123456789012345', 'pending', 1)"
        )
        db.execute(
            "CREATE TABLE instagram_publish_operations "
            "(operation_key TEXT PRIMARY KEY, container_id TEXT, status TEXT, updated_at REAL)"
        )
        db.execute(
            "INSERT INTO instagram_publish_operations VALUES "
            "('operation-123456789012345', 'container-123456789012345', 'publishing', 1)"
        )
    calls: list[str] = []
    monkeypatch.setattr(
        "amn.operator_alerts._send_telegram",
        lambda token, chat, message, timeout: calls.append(message),
    )

    result = check_operator_alerts(
        database,
        bot_token="token",
        chat_id="@ops",
        stale_after_seconds=60,
        now=1000,
    )

    assert result["active"] == 2
    assert result["sent"] == 2
    assert any("TikTok" in message for message in calls)
    assert any("manual reconciliation" in message for message in calls)


def test_telegram_notification_failure_is_non_fatal_and_sanitized(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    queue = JobQueue(database)
    job_id = queue.enqueue("pipeline", max_attempts=1)
    assert queue.claim("worker") is not None
    assert queue.fail(job_id, "worker", "do not expose this error", retry_delay=0)

    def fail_send(*args, **kwargs) -> None:
        raise RuntimeError("request failed with token=secret-value")

    monkeypatch.setattr("amn.operator_alerts._send_telegram", fail_send)
    result = check_operator_alerts(database, bot_token="secret-value", chat_id="@ops", now=3000)

    assert result["failed"] == 1
    alert = operator_alert_snapshot(database)["recent"][0]
    assert alert["last_error"] == "notification failed (RuntimeError)"
    assert "secret-value" not in str(alert)
    assert "do not expose this error" not in str(alert)


def test_alerts_cli_uses_private_alert_chat_not_publishing_chat(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    queue = JobQueue(database)
    job_id = queue.enqueue("publish", max_attempts=1)
    assert queue.claim("worker") is not None
    assert queue.fail(job_id, "worker", "provider failure", retry_delay=0)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@public-channel")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "@private-ops")
    destinations: list[str] = []
    monkeypatch.setattr(
        "amn.operator_alerts._send_telegram",
        lambda token, chat, message, timeout: destinations.append(chat),
    )

    assert main(["alerts", "check", "--root", str(tmp_path)]) == 0
    assert destinations == ["@private-ops"]


def test_alerts_can_run_from_the_durable_schedule_worker(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    queue = JobQueue(database)
    failed_job = queue.enqueue("publish", max_attempts=1)
    assert queue.claim("worker-a") is not None
    assert queue.fail(failed_job, "worker-a", "provider failure", retry_delay=0)
    queue.add_schedule(
        "operator-alerts",
        "alerts",
        {"root": str(tmp_path)},
        300,
        first_run_at=1000,
    )
    assert queue.enqueue_due_schedules(now=1000) == 1

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "@private-ops")
    messages: list[str] = []
    monkeypatch.setattr(
        "amn.operator_alerts._send_telegram",
        lambda token, chat, message, timeout: messages.append(message),
    )

    assert main(["jobs", "worker", "--once", "--root", str(tmp_path)]) == 0
    assert len(messages) == 1
    assert "AMN job permanently failed" in messages[0]
    assert queue.counts()["succeeded"] == 1

def test_stalled_queue_jobs_and_expired_leases_are_alerted(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    queue = JobQueue(database)
    expired_id = queue.enqueue("publish", max_attempts=3)
    queued_id = queue.enqueue("fetch", max_attempts=3)
    assert queue.claim("worker-that-disappeared", lease_seconds=30) is not None

    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE jobs SET lease_until = 900, updated_at = 800 WHERE id = ?",
            (expired_id,),
        )
        db.execute(
            "UPDATE jobs SET available_at = 1, updated_at = 800 WHERE id = ?",
            (queued_id,),
        )

    messages: list[str] = []
    monkeypatch.setattr(
        "amn.operator_alerts._send_telegram",
        lambda token, chat, message, timeout: messages.append(message),
    )

    result = check_operator_alerts(
        database,
        bot_token="token",
        chat_id="@ops",
        stale_after_seconds=60,
        now=1000,
    )

    assert result["active"] == 2
    assert result["sent"] == 2
    assert any("worker lease expired" in message for message in messages)
    assert any("ready for 3 minutes" in message for message in messages)
    assert expired_id[:8] in " ".join(messages)
    assert queued_id[:8] in " ".join(messages)


def test_pending_jobs_not_yet_available_do_not_trigger_stall_alert(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    queue = JobQueue(database)
    job_id = queue.enqueue("fetch", delay=3600)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE jobs SET updated_at = 1 WHERE id = ?", (job_id,))

    calls: list[str] = []
    monkeypatch.setattr(
        "amn.operator_alerts._send_telegram",
        lambda token, chat, message, timeout: calls.append(message),
    )

    result = check_operator_alerts(
        database,
        bot_token="token",
        chat_id="@ops",
        stale_after_seconds=60,
        now=1000,
    )

    assert result["active"] == 0
    assert result["sent"] == 0
    assert calls == []

def test_same_timestamp_alert_checks_do_not_resend_another_run_claim(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    queue = JobQueue(database)
    job_id = queue.enqueue("publish", max_attempts=1)
    assert queue.claim("worker") is not None
    assert queue.fail(job_id, "worker", "provider failure", retry_delay=0)

    calls: list[str] = []
    monkeypatch.setattr(
        "amn.operator_alerts._send_telegram",
        lambda token, chat, message, timeout: calls.append(message),
    )

    first = check_operator_alerts(database, bot_token="token", chat_id="@ops", now=1000)
    second = check_operator_alerts(database, bot_token="token", chat_id="@ops", now=1000)

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert len(calls) == 1

