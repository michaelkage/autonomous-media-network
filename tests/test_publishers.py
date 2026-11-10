from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from amn.publish import RateLimiter
from amn.publishers.instagram import InstagramPublisher
from amn.publishers.telegram import TelegramPublisher
from amn.publishers.tiktok import TikTokPublisher


def _response(payload: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.headers = {}
    response.raise_for_status.return_value = None
    return response


def test_telegram_exposes_provider_retry_after(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    response = _response({"parameters": {"retry_after": 7}}, status_code=429)
    publisher = TelegramPublisher("token", "@channel")
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
    assert publisher.retry_after == 7


def test_instagram_runs_container_processing_and_publish_flow(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = InstagramPublisher("token", "123456", "https://media.example.com/outbox")
    publisher.session.post = MagicMock(
        side_effect=[
            _response({"id": "container-1"}),
            _response({"id": "published-1"}),
        ]
    )
    publisher.session.get = MagicMock(return_value=_response({"status_code": "FINISHED"}))

    assert publisher.publish(image, "caption") is True
    assert publisher.session.post.call_count == 2
    assert publisher.session.get.call_count == 1


def test_instagram_requires_public_https_media_host() -> None:
    import pytest

    with pytest.raises(ValueError, match="public HTTPS"):
        InstagramPublisher("token", "123456", "http://localhost/outbox")


def test_tiktok_photo_publish_checks_creator_privacy_and_status(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = TikTokPublisher(
        "token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
    )
    publisher.session.post = MagicMock(
        side_effect=[
            _response({"data": {"privacy_level_options": ["SELF_ONLY"]}}),
            _response({"data": {"publish_id": "publish-1"}}),
            _response({"data": {"status": "PUBLISH_COMPLETE"}}),
        ]
    )

    with patch.object(RateLimiter, "wait", return_value=None):
        assert publisher.publish(image, "caption") is True

    assert publisher.session.post.call_count == 3


def test_tiktok_rejects_unapproved_privacy_level(tmp_path: Path) -> None:
    import pytest

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = TikTokPublisher(
        "token",
        "https://media.example.com/outbox",
        "PUBLIC_TO_EVERYONE",
        direct_post_enabled=True,
    )
    publisher.session.post = MagicMock(
        return_value=_response({"data": {"privacy_level_options": ["SELF_ONLY"]}})
    )

    with patch.object(RateLimiter, "wait", return_value=None):
        assert publisher.publish(image, "caption") is False
