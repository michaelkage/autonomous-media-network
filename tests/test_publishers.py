from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from amn.publish import RateLimiter
from amn.publishers.instagram import InstagramPublisher
from amn.publishers.telegram import TelegramPublisher
from amn.publishers.tiktok import SQLiteTokenRateLimiter, TikTokPublisher


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


def test_telegram_rejects_photo_over_bot_api_upload_limit(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    with image.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    publisher = TelegramPublisher("token", "@channel")
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_not_called()


def test_telegram_uses_image_mime_type(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = TelegramPublisher("token", "@channel")
    publisher.session.post = MagicMock(return_value=_response({"ok": True}))

    assert publisher.publish(image, "caption") is True
    uploaded = publisher.session.post.call_args.kwargs["files"]["photo"]
    assert uploaded[2] == "image/jpeg"


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


def test_instagram_rejects_image_over_graph_api_limit(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    with image.open("wb") as handle:
        handle.truncate(8 * 1024 * 1024 + 1)
    publisher = InstagramPublisher("token", "123456", "https://media.example.com/outbox")
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_not_called()


def test_instagram_requires_public_https_media_host() -> None:
    with pytest.raises(ValueError, match="public HTTPS"):
        InstagramPublisher("token", "123456", "http://localhost/outbox")


def test_tiktok_publishers_share_rate_limiter_per_access_token() -> None:
    first = TikTokPublisher(
        "shared-token-for-rate-limit-test",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
    )
    second = TikTokPublisher(
        "shared-token-for-rate-limit-test",
        "https://media.example.com/other",
        "SELF_ONLY",
        direct_post_enabled=True,
    )
    other = TikTokPublisher(
        "different-token-for-rate-limit-test",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
    )

    assert first.rate_limiter is second.rate_limiter
    assert first.rate_limiter is not other.rate_limiter


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


def test_tiktok_sqlite_rate_limiter_coordinates_publisher_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "jobs.sqlite3"
    first = SQLiteTokenRateLimiter(database, "hashed-token", min_interval=10)
    second = SQLiteTokenRateLimiter(database, "hashed-token", min_interval=10)
    sleeps: list[float] = []
    clock = iter([100.0, 100.0])
    monkeypatch.setattr("amn.publishers.tiktok.time.time", lambda: next(clock))
    monkeypatch.setattr("amn.publishers.tiktok.time.sleep", sleeps.append)
    first.wait()
    second.wait()
    assert sleeps == [10.0]


def test_tiktok_resumes_pending_publish_after_restart(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"same-photo-bytes")
    database = tmp_path / "jobs.sqlite3"
    first = TikTokPublisher(
        "recovery-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        rate_limit_db=database,
        max_polls=1,
        poll_interval=0,
    )
    first.session.post = MagicMock(
        side_effect=[
            _response({"data": {"privacy_level_options": ["SELF_ONLY"]}}),
            _response({"data": {"publish_id": "durable-publish-id"}}),
            _response({"data": {"status": "PROCESSING_DOWNLOAD"}}),
        ]
    )

    with patch("amn.publishers.tiktok.SQLiteTokenRateLimiter.wait", return_value=None):
        assert first.publish(image, "caption") is False

    restarted = TikTokPublisher(
        "recovery-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        rate_limit_db=database,
        max_polls=1,
        poll_interval=0,
    )
    restarted.session.post = MagicMock(
        return_value=_response({"data": {"status": "PUBLISH_COMPLETE"}})
    )

    with patch("amn.publishers.tiktok.SQLiteTokenRateLimiter.wait", return_value=None):
        assert restarted.publish(image, "caption") is True

    assert restarted.session.post.call_count == 1
    assert restarted.session.post.call_args.kwargs["json"] == {"publish_id": "durable-publish-id"}


def test_tiktok_completed_operation_is_not_submitted_again(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"same-photo-bytes")
    database = tmp_path / "jobs.sqlite3"
    publisher = TikTokPublisher(
        "completed-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        rate_limit_db=database,
    )
    publisher._save_operation("operation-key", "publish-id", "complete")
    publisher._operation_key = lambda *_args: "operation-key"
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is True
    publisher.session.post.assert_not_called()


def test_instagram_resumes_existing_container_after_restart(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"same-photo-bytes")
    database = tmp_path / "jobs.sqlite3"
    first = InstagramPublisher(
        "recovery-token",
        "123456",
        "https://media.example.com/outbox",
        operation_db=database,
        max_polls=1,
    )
    first.session.post = MagicMock(return_value=_response({"id": "container-1"}))
    first.session.get = MagicMock(return_value=_response({"status_code": "IN_PROGRESS"}))

    assert first.publish(image, "caption") is False
    first.session.post.assert_called_once()

    restarted = InstagramPublisher(
        "recovery-token",
        "123456",
        "https://media.example.com/outbox",
        operation_db=database,
        max_polls=1,
    )
    restarted.session.get = MagicMock(return_value=_response({"status_code": "FINISHED"}))
    restarted.session.post = MagicMock(return_value=_response({"id": "published-1"}))

    assert restarted.publish(image, "caption") is True
    restarted.session.post.assert_called_once()
    assert restarted.session.post.call_args.args[0].endswith("/media_publish")
    assert restarted.session.post.call_args.kwargs["data"]["creation_id"] == "container-1"


def test_instagram_does_not_repeat_ambiguous_media_publish(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"same-photo-bytes")
    database = tmp_path / "jobs.sqlite3"
    first = InstagramPublisher(
        "ambiguous-token",
        "123456",
        "https://media.example.com/outbox",
        operation_db=database,
        max_polls=1,
    )
    first.session.post = MagicMock(
        side_effect=[_response({"id": "container-ambiguous"}), requests.Timeout("lost response")]
    )
    first.session.get = MagicMock(return_value=_response({"status_code": "FINISHED"}))

    assert first.publish(image, "caption") is False
    assert first.session.post.call_count == 2

    restarted = InstagramPublisher(
        "ambiguous-token",
        "123456",
        "https://media.example.com/outbox",
        operation_db=database,
        max_polls=1,
    )
    restarted.session.get = MagicMock(return_value=_response({"status_code": "FINISHED"}))
    restarted.session.post = MagicMock()

    assert restarted.publish(image, "caption") is False
    restarted.session.post.assert_not_called()
