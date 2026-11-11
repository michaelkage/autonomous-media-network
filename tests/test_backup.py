from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from amn.backup import backup_database, restore_database, verify_database
from amn.jobs import JobQueue


def test_backup_is_consistent_and_verifiable(tmp_path: Path) -> None:
    database = tmp_path / "build" / "jobs.sqlite3"
    queue = JobQueue(database)
    job_id = queue.enqueue("pipeline", {"root": "/workspace"})
    queue.add_schedule("hourly", "fetch", {"query": "earthporn"}, 3600)
    destination = tmp_path / "backups" / "amn.sqlite3"

    assert backup_database(database, destination) == destination.resolve()
    assert verify_database(destination) == destination.resolve()

    restored_queue = JobQueue(destination)
    assert restored_queue.get(job_id)["payload"] == {"root": "/workspace"}
    assert restored_queue.list_schedules()[0]["id"] == "hourly"


def test_backup_rejects_same_path_and_invalid_database(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobQueue(database)

    with pytest.raises(ValueError, match="differ"):
        backup_database(database, database)

    invalid = tmp_path / "not-a-database.sqlite3"
    invalid.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        verify_database(invalid)


def test_restore_requires_explicit_worker_shutdown_and_keeps_safety_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "backup.sqlite3"
    destination = tmp_path / "build" / "jobs.sqlite3"
    source_queue = JobQueue(source)
    source_job = source_queue.enqueue("pipeline", {"generation": "backup"})
    backup_database(source, tmp_path / "snapshot.sqlite3")
    snapshot = tmp_path / "snapshot.sqlite3"

    current_queue = JobQueue(destination)
    current_queue.enqueue("publish", {"generation": "current"})

    with pytest.raises(ValueError, match="confirm-no-workers"):
        restore_database(snapshot, destination)

    restored, safety_copy = restore_database(
        snapshot, destination, confirm_no_workers=True
    )

    assert restored == destination.resolve()
    assert safety_copy is not None and safety_copy.is_file()
    assert verify_database(safety_copy) == safety_copy.resolve()
    result = JobQueue(destination).list()
    assert len(result) == 1
    assert result[0]["id"] == source_job
    assert result[0]["payload"] == {"generation": "backup"}


def test_restore_refuses_active_leases(tmp_path: Path) -> None:
    source = tmp_path / "backup.sqlite3"
    backup_database(
        (lambda path: (JobQueue(path), path)[1])(tmp_path / "empty.sqlite3"),
        source,
    )
    destination = tmp_path / "current.sqlite3"
    queue = JobQueue(destination)
    queue.enqueue("pipeline")
    assert queue.claim("worker-a", lease_seconds=300) is not None

    with pytest.raises(ValueError, match="active"):
        restore_database(source, destination, confirm_no_workers=True)


def test_backup_cli_and_restore_cli(tmp_path: Path, capsys) -> None:
    from amn.pipeline import main

    root = tmp_path / "workspace"
    JobQueue(root / "build" / "jobs.sqlite3").enqueue("pipeline")
    backup = tmp_path / "backup.sqlite3"

    assert main(["jobs", "backup", "--output", str(backup), "--root", str(root)]) == 0
    assert "backup created" in capsys.readouterr().out
    assert main(["jobs", "verify-backup", "--input", str(backup), "--root", str(root)]) == 0
    assert "backup verified" in capsys.readouterr().out
