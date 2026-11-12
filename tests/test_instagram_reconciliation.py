from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from amn.delivery_events import delivery_event_snapshot
from amn.instagram_reconciliation import (
    instagram_resolution_history,
    list_instagram_reconciliation,
    resolve_instagram_operation,
)
from amn.pipeline import main


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "build" / "jobs.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE instagram_publish_operations ("
            "operation_key TEXT PRIMARY KEY, container_id TEXT NOT NULL, "
            "status TEXT NOT NULL, updated_at REAL NOT NULL, delivery_id TEXT, output TEXT)"
        )
        db.executemany(
            "INSERT INTO instagram_publish_operations "
            "(operation_key, container_id, status, updated_at, output) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("ambiguous-a", "container-a", "publishing", time.time(), "outbox/a.jpg"),
                ("ambiguous-b", "container-b", "publishing", time.time(), "outbox/b.jpg"),
                ("complete", "container-c", "complete", time.time(), "outbox/c.jpg"),
            ],
        )
    return database


def test_instagram_reconciliation_lists_only_ambiguous_operations(tmp_path: Path) -> None:
    database = _database(tmp_path)

    operations = list_instagram_reconciliation(database)

    assert [item["operation_key"] for item in operations] == ["ambiguous-a", "ambiguous-b"]
    assert operations[0]["container_id"] == "container-a"
    assert operations[0]["output"] == "outbox/a.jpg"
    assert list_instagram_reconciliation(tmp_path / "missing.sqlite3") == []


def test_instagram_resolution_published_is_atomic_and_audited(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE instagram_publish_operations SET delivery_id = ? WHERE operation_key = ?",
            ("original-manifest-delivery", "ambiguous-a"),
        )

    result = resolve_instagram_operation(
        database,
        operation_key="ambiguous-a",
        resolution="published",
        actor="operator@example.test",
        note="Verified the post is publicly available in the Instagram account.",
    )

    assert result["status"] == "complete"
    with sqlite3.connect(database) as db:
        status = db.execute(
            "SELECT status FROM instagram_publish_operations WHERE operation_key = ?",
            ("ambiguous-a",),
        ).fetchone()[0]
    assert status == "complete"

    history = instagram_resolution_history(database)
    assert len(history) == 1
    assert history[0]["operation_key"] == "ambiguous-a"
    assert history[0]["resolution"] == "published"
    assert history[0]["previous_status"] == "publishing"
    assert history[0]["new_status"] == "complete"
    assert history[0]["actor"] == "operator@example.test"
    assert "publicly available" in history[0]["note"]

    snapshot = delivery_event_snapshot(database)
    assert snapshot["instagram_operations"]["complete"] == 2
    assert snapshot["instagram_operations"]["reconciliation_required"] == 1
    assert snapshot["counts"]["manual_reconciliation_published"] == 1
    assert snapshot["recent"][0]["event_type"] == "manual_reconciliation_published"
    assert snapshot["recent"][0]["delivery_id"] == "original-manifest-delivery"
    audit = snapshot["instagram_operations"]["resolution_history"]
    assert audit[0]["operation_key"] == "ambiguous-a"


def test_instagram_not_published_resolution_marks_retryable_and_audits(tmp_path: Path) -> None:
    database = _database(tmp_path)

    result = resolve_instagram_operation(
        database,
        operation_key="ambiguous-b",
        resolution="not_published",
        actor="operator",
        note="Checked the account and confirmed no post exists.",
    )

    assert result["status"] == "failed"
    with sqlite3.connect(database) as db:
        status = db.execute(
            "SELECT status FROM instagram_publish_operations WHERE operation_key = ?",
            ("ambiguous-b",),
        ).fetchone()[0]
    assert status == "failed"
    assert instagram_resolution_history(database)[0]["resolution"] == "not_published"
    assert delivery_event_snapshot(database)["counts"]["manual_reconciliation_not_published"] == 1


def test_instagram_operation_cannot_be_resolved_twice(tmp_path: Path) -> None:
    database = _database(tmp_path)
    args = {
        "operation_key": "ambiguous-a",
        "resolution": "published",
        "actor": "operator",
        "note": "Verified publication.",
    }
    resolve_instagram_operation(database, **args)

    with pytest.raises(ValueError, match="not awaiting reconciliation"):
        resolve_instagram_operation(database, **args)

    assert len(instagram_resolution_history(database)) == 1


@pytest.mark.parametrize(
    ("operation_key", "resolution", "actor", "note", "message"),
    [
        ("missing", "published", "operator", "Verified.", "not found"),
        ("ambiguous-a", "unknown", "operator", "Verified.", "resolution must"),
        ("ambiguous-a", "published", "", "Verified.", "actor must"),
        ("ambiguous-a", "published", "operator", "", "note must"),
        ("ambiguous-a", "published", "operator", "access_token=secret", "credentials"),
    ],
)
def test_instagram_resolution_validates_operator_input(
    tmp_path: Path,
    operation_key: str,
    resolution: str,
    actor: str,
    note: str,
    message: str,
) -> None:
    database = _database(tmp_path)
    with pytest.raises(ValueError, match=message):
        resolve_instagram_operation(
            database,
            operation_key=operation_key,
            resolution=resolution,
            actor=actor,
            note=note,
        )


def test_instagram_reconciliation_cli_lists_and_resolves(tmp_path: Path, capsys) -> None:
    database = _database(tmp_path)

    assert main(["instagram-reconcile", "list", "--root", str(tmp_path)]) == 0
    listed = capsys.readouterr().out
    assert "ambiguous-a" in listed
    assert "container=container-a" in listed

    assert (
        main(
            [
                "instagram-reconcile",
                "resolve",
                "--root",
                str(tmp_path),
                "--operation-key",
                "ambiguous-a",
                "--resolution",
                "published",
                "--actor",
                "cli-operator",
                "--note",
                "Verified the live post.",
            ]
        )
        == 0
    )
    resolved = capsys.readouterr().out
    assert "resolution=published" in resolved
    assert "status=complete" in resolved

    with sqlite3.connect(database) as db:
        status = db.execute(
            "SELECT status FROM instagram_publish_operations WHERE operation_key = ?",
            ("ambiguous-a",),
        ).fetchone()[0]
    assert status == "complete"


def test_instagram_resolution_history_supports_later_retry_cycle(tmp_path: Path) -> None:
    database = _database(tmp_path)
    resolve_instagram_operation(
        database,
        operation_key="ambiguous-a",
        resolution="not_published",
        actor="operator",
        note="Confirmed no post was created.",
    )
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE instagram_publish_operations SET status = 'publishing' WHERE operation_key = ?",
            ("ambiguous-a",),
        )

    resolve_instagram_operation(
        database,
        operation_key="ambiguous-a",
        resolution="published",
        actor="operator",
        note="Verified the retried post is now live.",
    )

    history = instagram_resolution_history(database)
    assert len(history) == 2
    assert [item["resolution"] for item in history] == ["published", "not_published"]
