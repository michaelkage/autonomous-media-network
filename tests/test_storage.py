from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from amn.publish import Publisher
from amn.storage import S3MediaStorage, StorageBackedPublisher


class RecordingPublisher(Publisher):
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.retry_after = 12.0

    def publish(self, image_path: Path, caption: str) -> bool:
        self.calls.append((image_path, caption))
        return True


def test_s3_storage_uploads_to_public_url_prefix_and_caches(tmp_path: Path) -> None:
    image = tmp_path / "asset one.jpg"
    image.write_bytes(b"jpeg")
    client = MagicMock()
    storage = S3MediaStorage("amn-media", client=client)

    key = storage.upload(image, "https://cdn.example.com/outbox")

    assert key == "outbox/asset one.jpg"
    client.upload_file.assert_called_once_with(
        str(image.resolve()),
        "amn-media",
        "outbox/asset one.jpg",
        ExtraArgs={
            "ContentType": "image/jpeg",
            "CacheControl": "public, max-age=300",
        },
    )
    assert storage.upload(image, "https://cdn.example.com/outbox") == key
    client.upload_file.assert_called_once()


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example.com/outbox",
        "https://cdn.example.com/outbox?token=secret",
        "https://cdn.example.com/outbox#fragment",
        "https://cdn.example.com/../private",
    ],
)
def test_s3_storage_rejects_unsafe_public_urls(tmp_path: Path, url: str) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"jpeg")
    storage = S3MediaStorage("amn-media", client=MagicMock())

    with pytest.raises(ValueError):
        storage.upload(image, url)


def test_s3_storage_rejects_missing_files(tmp_path: Path) -> None:
    storage = S3MediaStorage("amn-media", client=MagicMock())

    with pytest.raises(FileNotFoundError):
        storage.upload(tmp_path / "missing.jpg", "https://cdn.example.com/outbox")


def test_storage_backed_publisher_uploads_before_remote_publish(tmp_path: Path) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"jpeg")
    storage = MagicMock()
    publisher = RecordingPublisher()
    backed = StorageBackedPublisher(publisher, storage, "https://cdn.example.com/outbox")

    assert backed.publish(image, "caption") is True

    storage.upload.assert_called_once_with(image, "https://cdn.example.com/outbox")
    assert publisher.calls == [(image, "caption")]
    assert backed.retry_after == 12.0


def test_s3_config_validation_rejects_unknown_fields() -> None:
    from amn.distribution import validate_config

    with pytest.raises(ValueError, match="unsupported storage fields"):
        validate_config(
            {
                "storage": {"provider": "s3", "bucket": "amn-media", "secret": "no"},
                "targets": [],
                "routes": {},
            }
        )


def test_routed_instagram_publishing_uploads_to_configured_storage(tmp_path: Path, monkeypatch) -> None:
    import json

    from amn.distribution import publish_routed_manifest
    from amn.publishers import instagram
    from amn.storage import S3MediaStorage

    (tmp_path / "config").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "outbox").mkdir()
    image = tmp_path / "outbox" / "asset.jpg"
    image.write_bytes(b"jpeg")
    config = {
        "storage": {"provider": "s3", "bucket": "amn-media"},
        "targets": [
            {
                "id": "ig",
                "platform": "instagram",
                "enabled": True,
                "instagram_user_id": "12345",
                "public_base_url": "https://cdn.example.com/outbox",
            }
        ],
        "routes": {"default": ["ig"]},
    }
    (tmp_path / "config" / "amn.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "build" / "manifest.json").write_text(
        json.dumps({"passed": [{"output": "outbox/asset.jpg", "caption": "caption"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "token")
    remote = RecordingPublisher()
    monkeypatch.setattr(instagram, "InstagramPublisher", lambda *args: remote)
    fake_storage = MagicMock()
    monkeypatch.setattr(
        S3MediaStorage,
        "from_config",
        classmethod(lambda cls, values: fake_storage),
    )

    assert publish_routed_manifest(tmp_path) == {"ig": 1}
    fake_storage.upload.assert_called_once_with(image.resolve(), "https://cdn.example.com/outbox")
    assert remote.calls == [(image.resolve(), "caption")]


def test_storage_backed_publisher_sanitizes_upload_failure(tmp_path: Path, capsys) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"jpeg")
    storage = MagicMock()
    storage.upload.side_effect = RuntimeError("sensitive provider response")
    publisher = RecordingPublisher()
    backed = StorageBackedPublisher(publisher, storage, "https://cdn.example.com/outbox")

    assert backed.publish(image, "caption") is False
    assert publisher.calls == []
    assert "sensitive provider response" not in capsys.readouterr().out
