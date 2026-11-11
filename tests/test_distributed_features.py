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
    assert limiter.retry_delay(1, retry_after=20) == 20


def test_pipeline_fails_closed_without_watermark(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    Image.new("RGB", (1200, 1200), "white").save(inbox / "asset.jpg")
    (inbox / "asset.json").write_text(json.dumps({"likes": 500}), encoding="utf-8")
    result = run(tmp_path, watermark_text="")
    assert result.errors
    assert "watermark required" in result.errors[0]


def test_config_rejects_invalid_target_platform_and_non_integer_thresholds() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        validate_config(
            {
                "targets": [{"id": "bad", "platform": "unknown"}],
                "routes": {},
                "thresholds": {"min_likes": 500, "max_distance": 5},
            }
        )
    with pytest.raises(ValueError, match="non-negative integer"):
        validate_config(
            {
                "targets": [],
                "routes": {},
                "thresholds": {"min_likes": True, "max_distance": 5},
            }
        )


def test_example_config_validates_with_disabled_tiktok_target() -> None:
    example = Path(__file__).resolve().parent.parent / "config" / "amn.example.json"
    assert validate_config(json.loads(example.read_text(encoding="utf-8")))


def test_enabled_tiktok_target_requires_explicit_direct_post() -> None:
    with pytest.raises(ValueError, match="direct_post_enabled"):
        validate_config(
            {
                "targets": [
                    {
                        "id": "tiktok-main",
                        "platform": "tiktok",
                        "enabled": True,
                        "public_base_url": "https://media.example.com/outbox",
                        "privacy_level": "SELF_ONLY",
                    }
                ],
                "routes": {},
                "thresholds": {"min_likes": 500, "max_distance": 5},
            }
        )


def test_storage_config_accepts_s3_with_environment_credentials() -> None:
    config = {
        "storage": {
            "provider": "s3",
            "bucket": "amn-media",
            "region": "auto",
            "endpoint_url_env": "AMN_S3_ENDPOINT_URL",
        },
        "targets": [],
        "routes": {},
    }
    assert validate_config(config) == config


def test_storage_config_rejects_invalid_provider_and_environment_names() -> None:
    with pytest.raises(ValueError, match="storage.provider"):
        validate_config(
            {
                "storage": {"provider": "gcs", "bucket": "amn-media"},
                "targets": [],
                "routes": {},
            }
        )
    with pytest.raises(ValueError, match="storage.access_key_env"):
        validate_config(
            {
                "storage": {
                    "provider": "s3",
                    "bucket": "amn-media",
                    "access_key_env": "NOT A VARIABLE",
                },
                "targets": [],
                "routes": {},
            }
        )


def test_reddit_target_requires_valid_subreddit_and_https_media() -> None:
    valid = {
        "targets": [
            {
                "id": "reddit-main",
                "platform": "reddit",
                "enabled": True,
                "subreddit": "earthporn",
                "public_base_url": "https://cdn.example.com/outbox",
            }
        ],
        "routes": {"default": ["reddit-main"]},
    }
    assert validate_config(valid) == valid

    invalid_subreddit = {
        **valid,
        "targets": [{**valid["targets"][0], "subreddit": "r/earthporn"}],
    }
    with pytest.raises(ValueError, match="subreddit"):
        validate_config(invalid_subreddit)

    invalid_url = {
        **valid,
        "targets": [{**valid["targets"][0], "public_base_url": "http://localhost/outbox"}],
    }
    with pytest.raises(ValueError, match="HTTPS"):
        validate_config(invalid_url)


def test_reddit_devvit_target_requires_subreddit_and_https_media() -> None:
    valid = {
        "targets": [
            {
                "id": "reddit-devvit",
                "platform": "reddit_devvit",
                "enabled": True,
                "subreddit": "earthporn",
                "public_base_url": "https://cdn.example.com/outbox",
                "bridge_url_env": "AMN_REDDIT_DEVVIT_URL",
                "bridge_token_env": "AMN_REDDIT_DEVVIT_TOKEN",
            }
        ],
        "routes": {"landscape": ["reddit-devvit"]},
    }
    assert validate_config(valid) == valid

    invalid = {
        **valid,
        "targets": [{**valid["targets"][0], "bridge_token_env": "not a variable"}],
    }
    with pytest.raises(ValueError, match="bridge_token_env"):
        validate_config(invalid)
