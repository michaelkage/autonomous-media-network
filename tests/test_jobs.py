from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from amn.jobs import JobQueue
from amn.pipeline import main

def test_enqueue_claim_and_complete_round_trip(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "build" / "jobs.sqlite3")
    job_id = queue.enqueue("pipeline", {"root": "/workspace"}, max_attempts=2)

    claimed = queue.claim("worker-a", lease_seconds=30)

    assert claimed is not None
    assert claimed["id"] == job_id
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["payload"] == {"root": "/workspace"}
    assert queue.complete(job_id, "wrong-worker") is False
    assert queue.complete(job_id, "worker-a") is True
    assert queue.get(job_id)["status"] == "succeeded"
    assert queue.counts() == {"pending": 0, "running": 0, "succeeded": 1, "failed": 0}

def test_enqueue_dedupe_key_returns_existing_job(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    first = queue.enqueue("fetch", {"source": "reddit"}, dedupe_key="fetch:reddit:today")
    second = queue.enqueue("fetch", {"source": "other"}, dedupe_key="fetch:reddit:today")

    assert first == second
    assert queue.counts()["pending"] == 1
    assert queue.get(first)["payload"] == {"source": "reddit"}

def test_failed_job_retries_then_becomes_terminal(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    job_id = queue.enqueue("publish", max_attempts=2)

    first = queue.claim("worker-a")
    assert first is not None
    assert queue.fail(job_id, "worker-a", "temporary provider error", retry_delay=0)

    second = queue.claim("worker-b")
    assert second is not None
    assert second["attempts"] == 2
    assert queue.fail(job_id, "worker-b", "permanent provider error", retry_delay=0)

    job = queue.get(job_id)
    assert job["status"] == "failed"
    assert job["last_error"] == "permanent provider error"
    assert queue.claim("worker-c") is None

def test_expired_lease_is_reclaimed_after_worker_crash(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    queue = JobQueue(database)
    job_id = queue.enqueue("pipeline", max_attempts=2)
    first = queue.claim("worker-that-crashed", lease_seconds=30)
    assert first is not None

    import sqlite3

    with sqlite3.connect(database) as db:
        db.execute("UPDATE jobs SET lease_until = 0 WHERE id = ?", (job_id,))

    recovered = queue.claim("recovery-worker", lease_seconds=30)

    assert recovered is not None
    assert recovered["id"] == job_id
    assert recovered["attempts"] == 2
    assert recovered["worker_id"] == "recovery-worker"

def test_expired_final_attempt_is_marked_failed(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    queue = JobQueue(database)
    job_id = queue.enqueue("publish", max_attempts=1)
    assert queue.claim("worker-a", lease_seconds=30) is not None

    import sqlite3

    with sqlite3.connect(database) as db:
        db.execute("UPDATE jobs SET lease_until = 0 WHERE id = ?", (job_id,))

    assert queue.claim("worker-b") is None
    assert queue.get(job_id)["status"] == "failed"

def test_heartbeat_extends_only_owned_running_job(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    job_id = queue.enqueue("pipeline")
    assert queue.claim("worker-a") is not None

    assert queue.heartbeat(job_id, "worker-b") is False
    assert queue.heartbeat(job_id, "worker-a", lease_seconds=60) is True

def test_concurrent_workers_cannot_claim_same_job(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue("pipeline")

    def claim(index: int):
        return queue.claim(f"worker-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))

    successful = [job for job in claims if job is not None]
    assert len(successful) == 1

def test_queue_validates_inputs_and_list_filters(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError):
        queue.enqueue("", {})
    with pytest.raises(ValueError):
        queue.enqueue("pipeline", [], max_attempts=1)
    with pytest.raises(ValueError):
        queue.enqueue("pipeline", {}, max_attempts=0)
    with pytest.raises(ValueError):
        queue.list(status="unknown")

    job_id = queue.enqueue("pipeline")
    assert queue.list(status="pending")[0]["id"] == job_id

def test_jobs_cli_enqueues_and_worker_completes_pipeline(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "inbox").mkdir()
    monkeypatch.setenv("AMN_WATERMARK_TEXT", "@amn")

    assert main(["jobs", "enqueue", "--kind", "pipeline", "--root", str(tmp_path)]) == 0
    assert main(["jobs", "worker", "--once", "--root", str(tmp_path)]) == 0

    queue = JobQueue(tmp_path / "build" / "jobs.sqlite3")
    assert queue.counts()["succeeded"] == 1
