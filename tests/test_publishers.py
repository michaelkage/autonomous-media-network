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


def test_telegram_persists_returned_message_id(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = TelegramPublisher("token", "@channel")
    publisher.session.post = MagicMock(
        return_value=_response({"ok": True, "result": {"message_id": 481}})
    )

    assert publisher.publish(image, "caption") is True
    assert publisher.last_message_id == 481


def test_telegram_clears_message_id_before_failed_send(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = TelegramPublisher("token", "@channel")
    publisher.last_message_id = 123
    publisher.session.post = MagicMock(return_value=_response({"ok": False, "description": "bad"}))

    assert publisher.publish(image, "caption") is False
    assert publisher.last_message_id is None


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


def test_instagram_archives_expired_container_before_retrying(tmp_path: Path) -> None:
    import sqlite3

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"same-photo-bytes")
    database = tmp_path / "jobs.sqlite3"
    publisher = InstagramPublisher(
        "expired-token",
        "123456",
        "https://media.example.com/outbox",
        operation_db=database,
        max_polls=1,
    )
    publisher.session.post = MagicMock(
        side_effect=[
            _response({"id": "expired-container"}),
            _response({"id": "replacement-container"}),
            _response({"id": "published-post"}),
        ]
    )
    publisher.session.get = MagicMock(
        side_effect=[
            _response({"status_code": "EXPIRED"}),
            _response({"status_code": "FINISHED"}),
        ]
    )

    assert publisher.publish(image, "caption") is False
    assert publisher.publish(image, "caption") is True

    with sqlite3.connect(database) as db:
        current = db.execute(
            "SELECT container_id, status FROM instagram_publish_operations"
        ).fetchone()
        history = db.execute(
            "SELECT container_id, event_kind, container_status FROM instagram_publish_history "
            "ORDER BY id"
        ).fetchall()

    assert current == ("replacement-container", "complete")
    assert history == [
        ("expired-container", "container_terminal", "EXPIRED"),
        ("expired-container", "container_replaced", "failed"),
    ]
    assert publisher.session.post.call_count == 3


def test_instagram_records_structured_provider_error_without_leaking_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sqlite3

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    database = tmp_path / "jobs.sqlite3"
    publisher = InstagramPublisher(
        "sensitive-access-token",
        "123456",
        "https://media.example.com/outbox",
        operation_db=database,
    )
    response = _response(
        {
            "error": {
                "message": "Invalid media URL",
                "type": "OAuthException",
                "code": 9007,
                "error_subcode": 2207052,
                "fbtrace_id": "trace-123",
            }
        },
        status_code=400,
    )
    response.raise_for_status.side_effect = requests.HTTPError("request failed", response=response)
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False

    with sqlite3.connect(database) as db:
        history = db.execute(
            "SELECT event_kind, phase, http_status, error_type, error_code, "
            "error_subcode, fbtrace_id, message FROM instagram_publish_history"
        ).fetchone()
    output = capsys.readouterr().out
    assert history == (
        "provider_error",
        "media_container_create",
        400,
        "OAuthException",
        "9007",
        "2207052",
        "trace-123",
        "Invalid media URL",
    )
    assert "9007" in output
    assert "sensitive-access-token" not in output
    assert "sensitive-access-token" not in str(history)


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
    restarted.session.get = MagicMock(return_value=_response({"status_code": "IN_PROGRESS"}))
    restarted.session.post = MagicMock()

    assert restarted.publish(image, "caption") is False
    restarted.session.post.assert_not_called()

    final_retry = InstagramPublisher(
        "ambiguous-token",
        "123456",
        "https://media.example.com/outbox",
        operation_db=database,
        max_polls=1,
    )
    final_retry.session.get = MagicMock(return_value=_response({"status_code": "FINISHED"}))
    final_retry.session.post = MagicMock()
    assert final_retry.publish(image, "caption") is False
    final_retry.session.post.assert_not_called()


def test_telegram_redacts_bot_token_from_http_error_logs(tmp_path: Path, capsys) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    token = "123456:private-bot-token"
    publisher = TelegramPublisher(token, "@channel")
    response = _response({"ok": False})
    response.raise_for_status.side_effect = requests.HTTPError(
        f"401 Client Error for url: https://api.telegram.org/bot{token}/sendPhoto"
    )
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
    output = capsys.readouterr().out
    assert token not in output
    assert "[REDACTED]" in output


def test_tiktok_redacts_access_token_from_provider_error_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    token = "sensitive-tiktok-access-token"
    publisher = TikTokPublisher(
        token,
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        max_polls=0,
    )
    publisher.rate_limiter.wait = MagicMock()
    response = _response({"error": {"code": "invalid", "message": f"rejected {token}"}})
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
    output = capsys.readouterr().out
    assert token not in output
    assert "[REDACTED]" in output

def test_tiktok_rejects_malformed_provider_json_without_crashing(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = TikTokPublisher(
        "test-access-token",
        "https://media.example.com/outbox",
        "SELF_ONLY",
        direct_post_enabled=True,
        max_polls=0,
    )
    publisher.rate_limiter.wait = MagicMock()
    response = _response({})
    response.json.return_value = []
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
