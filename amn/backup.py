"""Atomic SQLite backup, validation, and restore helpers for AMN state."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

_REQUIRED_TABLES = {"jobs", "schedules"}


def _validate_connection(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        detail = integrity[0] if integrity else "no integrity result"
        raise ValueError(f"SQLite integrity check failed: {detail}")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = _REQUIRED_TABLES - tables
    if missing:
        raise ValueError(f"not an AMN queue database; missing tables: {', '.join(sorted(missing))}")


def verify_database(database: Path) -> Path:
    """Validate SQLite integrity and the core AMN queue schema."""
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            _validate_connection(connection)
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"database is unreadable: {exc}") from exc
    return path


def _atomic_database_copy(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(source) as source_db, sqlite3.connect(temporary) as target_db:
            source_db.backup(target_db)
            _validate_connection(target_db)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def backup_database(source: Path, destination: Path) -> Path:
    """Create a consistent online SQLite backup and atomically publish it."""
    source_path = verify_database(source)
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("backup destination must differ from the source database")
    return _atomic_database_copy(source_path, destination_path)


def restore_database(
    source: Path,
    destination: Path,
    *,
    confirm_no_workers: bool = False,
) -> tuple[Path, Path | None]:
    """Restore a validated backup, retaining a timestamped copy of current state."""
    if not confirm_no_workers:
        raise ValueError("restore requires --confirm-no-workers after all AMN workers are stopped")
    source_path = verify_database(source)
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("restore source must differ from the destination database")

    if destination_path.is_file():
        try:
            with sqlite3.connect(f"{destination_path.as_uri()}?mode=ro", uri=True) as current:
                active = current.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'running' AND lease_until > ?",
                    (time.time(),),
                ).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"current database is unreadable: {exc}") from exc
        if active:
            raise ValueError(f"refusing restore while {active} job lease(s) are active")

    safety_copy: Path | None = None
    if destination_path.is_file():
        from datetime import UTC, datetime

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safety_copy = destination_path.with_name(f"{destination_path.name}.pre-restore-{stamp}.bak")
        suffix = 1
        while safety_copy.exists():
            safety_copy = destination_path.with_name(
                f"{destination_path.name}.pre-restore-{stamp}-{suffix}.bak"
            )
            suffix += 1
        backup_database(destination_path, safety_copy)

    _atomic_database_copy(source_path, destination_path)
    Path(f"{destination_path}-wal").unlink(missing_ok=True)
    Path(f"{destination_path}-shm").unlink(missing_ok=True)
    return destination_path, safety_copy
