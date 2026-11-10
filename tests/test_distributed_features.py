from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from amn.distribution import targets_for, validate_config
from amn.pipeline import run
from amn.publish import Publisher, RateLimiter, publish_manifest


class RecordingPublisher(Publisher):
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def publish(self, image_path: Path, caption: str) -> bool:
        self.calls.append(image_path)
        return True


def test_routes_only_explicitly_selected_targets() -> None:
    config = {
        "targets": [
            {"id": "tg-main", "platform": "telegram", "enabled": True},
            {"id": "tg-off", "platform": "telegram", "enabled": False},
        ],
        "routes": {"default": [], "landscape": ["tg-main", "tg-off"]},
    }
    assert [item["id"] for item in targets_for({"categories": ["landscape"]}, config)] == [
        "tg-main"
    ]
    assert targets_for({"categories": ["portrait"]}, config) == []


def test_rejects_unsafe_or_duplicate_target_ids() -> None:
    with pytest.raises(ValueError):
        validate_config(
            {
                "targets": [{"id": "../escape", "platform": "telegram"}],
                "routes": {"default": []},
            }
        )


def test_publish_manifest_resolves_root_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path
    (root / "outbox").mkdir()
    image_path = root / "outbox" / "asset.jpg"
    Image.new("RGB", (16, 16), "white").save(image_path)
    (root / "build").mkdir()
    manifest = root / "build" / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"passed": [{"output": "outbox/asset.jpg", "caption": "hello", "sha256": "abc123"}]}
        ),
        encoding="utf-8",
    )
    publisher = RecordingPublisher()

    assert publish_manifest(manifest, publisher, root=root, platform="test") == 1
    assert publish_manifest(manifest, publisher, root=root, platform="test") == 0
    assert publisher.calls == [image_path]


def test_publish_manifest_rejects_paths_outside_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.jpg"
    Image.new("RGB", (8, 8), "white").save(outside)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"passed": [{"output": str(outside), "caption": "nope"}]}),
        encoding="utf-8",
    )
    publisher = RecordingPublisher()
    assert publish_manifest(manifest, publisher, root=tmp_path) == 0
    assert publisher.calls == []


def test_rate_limiter_retry_delay_is_bounded() -> None:
    limiter = RateLimiter(min_interval=0, max_backoff=4)
    assert limiter.retry_delay(10) <= 4
    assert limiter.retry_delay(1, retry_after=20) == 4


def test_pipeline_fails_closed_without_watermark(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    Image.new("RGB", (1200, 1200), "white").save(inbox / "asset.jpg")
    (inbox / "asset.json").write_text(json.dumps({"likes": 500}), encoding="utf-8")
    result = run(tmp_path, watermark_text="")
    assert result.errors
    assert "watermark required" in result.errors[0]
