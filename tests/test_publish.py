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


def test_delivery_event_snapshot_validates_limit(tmp_path: Path) -> None:
    from amn.delivery_events import delivery_event_snapshot

    with pytest.raises(ValueError, match="between 1 and 500"):
        delivery_event_snapshot(tmp_path / "missing.sqlite3", limit=0)
