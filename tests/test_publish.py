"""Tests for the publishing layer. Never touches the network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amn.publish import BlueskyPublisher, MockPublisher, Publisher, publish_manifest


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

        assert publish_manifest(manifest, HalfFails()) == 1

    def test_missing_manifest_is_zero(self, tmp_path: Path):
        assert publish_manifest(tmp_path / "nope.json", MockPublisher()) == 0

    def test_no_passing_assets_is_zero(self, tmp_path: Path):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"passed": []}), encoding="utf-8")
        assert publish_manifest(path, MockPublisher()) == 0


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
