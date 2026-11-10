from __future__ import annotations

import sqlite3
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


def test_pipeline_jobs_are_serialized_across_workers(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    first_id = queue.enqueue("pipeline")
    second_id = queue.enqueue("pipeline")

    first = queue.claim("worker-a", lease_seconds=60)

    assert first is not None and first["id"] == first_id
    assert queue.claim("worker-b", lease_seconds=60) is None
    assert queue.complete(first_id, "worker-a")
    second = queue.claim("worker-b", lease_seconds=60)
    assert second is not None and second["id"] == second_id



def test_schedule_enqueues_due_job_and_skips_missed_intervals(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.add_schedule(
        "hourly-fetch",
        "fetch",
        {"query": "earthporn", "limit": 5},
        3600,
        first_run_at=1000,
    )

    assert queue.enqueue_due_schedules(now=1000) == 1
    assert queue.enqueue_due_schedules(now=1000) == 0
    assert queue.enqueue_due_schedules(now=10_000) == 1

    jobs = queue.list(limit=10)
    assert len(jobs) == 2
    assert all(job["kind"] == "fetch" for job in jobs)
    assert all(job["payload"]["query"] == "earthporn" for job in jobs)
    schedule = queue.list_schedules()[0]
    assert schedule["next_run_at"] > 10_000
    assert schedule["enabled"] is True


def test_schedule_can_be_disabled_and_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.add_schedule("nightly", "pipeline", {}, 86_400, first_run_at=0)

    with pytest.raises(ValueError, match="already exists"):
        queue.add_schedule("nightly", "pipeline", {}, 3600)

    assert queue.disable_schedule("nightly") is True
    assert queue.disable_schedule("nightly") is False
    assert queue.enqueue_due_schedules(now=100_000) == 0
    assert queue.list_schedules()[0]["enabled"] is False


def test_schedule_cli_add_list_tick_and_disable(tmp_path: Path, capsys) -> None:
    root = str(tmp_path)
    assert main(
        [
            "jobs", "schedule", "add", "--id", "daily-pipeline",
            "--kind", "pipeline", "--every-seconds", "86400", "--root", root,
        ]
    ) == 0
    assert main(["jobs", "schedule", "list", "--root", root]) == 0
    assert "daily-pipeline enabled pipeline" in capsys.readouterr().out

    queue = JobQueue(tmp_path / "build" / "jobs.sqlite3")
    with sqlite3.connect(queue.database) as db:
        db.execute("UPDATE schedules SET next_run_at = 0 WHERE id = ?", ("daily-pipeline",))

    assert main(["jobs", "schedule", "tick", "--root", root]) == 0
    assert queue.counts()["pending"] == 1
    assert main(
        ["jobs", "schedule", "disable", "--id", "daily-pipeline", "--root", root]
    ) == 0
