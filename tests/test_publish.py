"""Tests for the publishing layer. Never touches the network."""

from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import pytest

from amn.publish import BlueskyPublisher, MockPublisher, Publisher, publish_manifest


def _publish_from_process(manifest_path: str, root: str, marker_path: str, start_event) -> None:
    from amn.publish import Publisher, publish_manifest

    class SlowPublisher(Publisher):
        def publish(self, image_path: Path, caption: str) -> bool:
            with Path(marker_path).open("a", encoding="utf-8") as handle:
                handle.write(f"{image_path.name}\n")
                handle.flush()
            time.sleep(0.3)
            return True

    start_event.wait(timeout=10)
    publish_manifest(
        Path(manifest_path),
        SlowPublisher(),
        root=Path(root),
        platform="multiprocess-test",
        retries=0,
    )


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    """A manifest with two passing assets, written out as real files."""
    (tmp_path / "outbox").mkdir()
    for name in ("one.jpg", "two.jpg"):
        (tmp_path / "outbox" / name).write_bytes(b"not really a jpeg")
    payload = {
        "passed": [
            {"output": "outbox/one.jpg", "caption": "first"},
            {"output": "outbox/two.jpg"},
        ],
        "rejected": [],
        "errors": [],
    }
    path = tmp_path / "build" / "manifest.json"
    path.parent.mkdir()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestPublishManifest:
    def test_publishes_every_passing_asset(self, manifest: Path, tmp_path: Path):
        published = []

        class Recording(MockPublisher):
            def publish(self, image_path: Path, caption: str) -> bool:
                published.append((image_path, caption))
                return True

        count = publish_manifest(manifest, Recording())
        assert count == 2
        assert [p[0].name for p in published] == ["one.jpg", "two.jpg"]

    def test_uses_manifest_caption(self, manifest: Path):
        seen = {}

        class Recording(MockPublisher):
            def publish(self, image_path: Path, caption: str) -> bool:
                seen[image_path.name] = caption
                return True

        publish_manifest(manifest, Recording())
        assert seen["one.jpg"] == "first"

    def test_falls_back_to_default_caption(self, manifest: Path):
        seen = {}

        class Recording(MockPublisher):
            def publish(self, image_path: Path, caption: str) -> bool:
                seen[image_path.name] = caption
                return True

        publish_manifest(manifest, Recording())
        assert seen["two.jpg"] == "No caption provided."

    def test_counts_only_successes(self, manifest: Path):
        class HalfFails(MockPublisher):
            def publish(self, image_path: Path, caption: str) -> bool:
                return image_path.name == "one.jpg"

        assert publish_manifest(manifest, HalfFails(), retries=0) == 1

    def test_missing_manifest_is_zero(self, tmp_path: Path):
        assert publish_manifest(tmp_path / "nope.json", MockPublisher()) == 0

    def test_no_passing_assets_is_zero(self, tmp_path: Path):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"passed": []}), encoding="utf-8")
        assert publish_manifest(path, MockPublisher()) == 0

    def test_concurrent_processes_publish_each_delivery_once(self, manifest: Path, tmp_path: Path):
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        marker = tmp_path / "published.txt"
        processes = [
            context.Process(
                target=_publish_from_process,
                args=(str(manifest), str(tmp_path), str(marker), start_event),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0

        assert marker.read_text(encoding="utf-8").splitlines() == ["one.jpg", "two.jpg"]


class TestMockPublisher:
    def test_reports_success(self, tmp_path: Path, capsys):
        assert MockPublisher().publish(tmp_path / "x.jpg", "caption") is True
        assert "x.jpg" in capsys.readouterr().out


class TestBlueskyPublisher:
    def test_missing_dependency_gives_actionable_error(self):
        try:
            import atproto  # noqa: F401
        except ModuleNotFoundError:
            with pytest.raises(RuntimeError, match=r"\[bluesky\]"):
                BlueskyPublisher("me.bsky.social", "app-password")
        else:
            pytest.skip("atproto installed; dependency guard not exercised")

    def test_is_a_publisher(self):
        assert issubclass(BlueskyPublisher, Publisher)


def test_delivery_journal_records_success_without_caption_or_secrets(
    manifest: Path, tmp_path: Path
) -> None:
    from amn.delivery_events import delivery_event_snapshot

    class Recording(Publisher):
        def publish(self, image_path: Path, caption: str) -> bool:
            return True

    assert publish_manifest(manifest, Recording(), platform="journal-test") == 2
    snapshot = delivery_event_snapshot(tmp_path / "build" / "jobs.sqlite3")

    assert snapshot["counts"] == {
        "attempt_started": 2,
        "provider_accepted": 2,
        "published": 2,
    }
    assert [event["event_type"] for event in reversed(snapshot["recent"])] == [
        "attempt_started",
        "provider_accepted",
        "published",
        "attempt_started",
        "provider_accepted",
        "published",
    ]
    serialized = json.dumps(snapshot)
    assert "first" not in serialized
    assert "No caption provided" not in serialized
    assert "Authorization" not in serialized


def test_telegram_provider_message_id_is_recorded_in_delivery_journal(
    manifest: Path, tmp_path: Path
) -> None:
    from amn.delivery_events import delivery_event_snapshot

    class TelegramLike(Publisher):
        last_message_id = 481

        def publish(self, image_path: Path, caption: str) -> bool:
            return True

    assert publish_manifest(manifest, TelegramLike(), platform="telegram") == 2
    snapshot = delivery_event_snapshot(tmp_path / "build" / "jobs.sqlite3")
    accepted = [event for event in snapshot["recent"] if event["event_type"] == "provider_accepted"]
    assert {event["provider_message_id"] for event in accepted} == {"481"}


def test_delivery_journal_records_retry_and_terminal_failure(
    manifest: Path, tmp_path: Path, monkeypatch
) -> None:
    from amn.delivery_events import delivery_event_snapshot

    monkeypatch.setattr("amn.publish.time.sleep", lambda _delay: None)

    class AlwaysFails(Publisher):
        retry_after = 3.5

        def publish(self, image_path: Path, caption: str) -> bool:
            return False

    assert publish_manifest(manifest, AlwaysFails(), platform="retry-journal-test", retries=1) == 0
    snapshot = delivery_event_snapshot(tmp_path / "build" / "jobs.sqlite3")

    events = [event for event in snapshot["recent"] if event["output"] == "outbox/one.jpg"]
    chronological = list(reversed(events))
    assert [event["event_type"] for event in chronological] == [
        "attempt_started",
        "attempt_failed",
        "retry_scheduled",
        "attempt_started",
        "attempt_failed",
    ]
    assert chronological[1]["retry_after_seconds"] == 3.5
    assert chronological[1]["will_retry"] is True
    assert chronological[2]["retry_after_seconds"] == 3.5
    assert chronological[2]["will_retry"] is True
    assert chronological[-1]["will_retry"] is False


def test_delivery_event_snapshot_reads_legacy_schema_and_writer_migrates(
    tmp_path: Path,
) -> None:
    import sqlite3

    from amn.delivery_events import delivery_event_snapshot, record_delivery_event

    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(
            """
            CREATE TABLE delivery_events (
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
        db.execute(
            """
            INSERT INTO delivery_events (
                delivery_id, platform, output, event_type, attempt, created_at
            ) VALUES ('legacy-id', 'telegram', 'outbox/old.jpg', 'published', 1, 1)
            """
        )

    before = delivery_event_snapshot(database)
    assert before["recent"][0]["provider_message_id"] is None

    record_delivery_event(
        database,
        platform="telegram",
        delivery_key="new-delivery",
        output="outbox/new.jpg",
        event_type="provider_accepted",
        attempt=1,
        provider_message_id=481,
    )
    after = delivery_event_snapshot(database)
    assert after["recent"][0]["provider_message_id"] == "481"


def test_delivery_event_snapshot_validates_limit(tmp_path: Path) -> None:
    from amn.delivery_events import delivery_event_snapshot

    with pytest.raises(ValueError, match="between 1 and 500"):
        delivery_event_snapshot(tmp_path / "missing.sqlite3", limit=0)



def test_rate_limiter_rejects_non_finite_or_negative_configuration() -> None:
    from amn.publish import RateLimiter

    for kwargs in (
        {"min_interval": -1},
        {"min_interval": float("inf")},
        {"max_backoff": -1},
        {"max_backoff": float("nan")},
    ):
        with pytest.raises(ValueError):
            RateLimiter(**kwargs)


def test_rate_limiter_ignores_non_finite_retry_after() -> None:
    from amn.publish import RateLimiter

    limiter = RateLimiter(min_interval=0, max_backoff=5)
    assert 0 <= limiter.retry_delay(0, float("inf")) <= 2
    assert 0 <= limiter.retry_delay(0, float("nan")) <= 2


def test_retry_after_parser_rejects_non_finite_header() -> None:
    from amn.publish import _retry_after_from_response

    class Response:
        headers = {"Retry-After": "inf"}

    assert _retry_after_from_response(Response()) is None


@pytest.mark.parametrize("retries", [-1, True, 1.5, "2"])
def test_publish_manifest_rejects_invalid_retry_count(manifest: Path, retries) -> None:
    with pytest.raises(ValueError, match="retries must be a non-negative integer"):
        publish_manifest(manifest, MockPublisher(), retries=retries)
