from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from amn.publishers.reddit_devvit import RedditDevvitPublisher


def _response(payload: dict, status_code: int = 200, headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _publisher() -> RedditDevvitPublisher:
    publisher = RedditDevvitPublisher(
        "https://bridge.example.dev/external/amn/publish",
        "devvit_at_sensitive-token",
        "earthporn",
        "https://cdn.example.com/amn/outbox",
    )
    publisher.delivery_id = "a" * 64
    return publisher


def test_devvit_bridge_returns_native_post_receipt(tmp_path: Path) -> None:
    image = tmp_path / "asset photo.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    publisher.session.post = MagicMock(
        return_value=_response(
            {
                "status": "published",
                "post_id": "abc123",
                "permalink": "/r/earthporn/comments/abc123/title/",
            }
        )
    )

    assert publisher.publish(image, "A beautiful landscape") is True
    assert publisher.last_message_id == "abc123"
    assert publisher.last_permalink == "/r/earthporn/comments/abc123/title/"
    call = publisher.session.post.call_args
    assert call.args[0] == "https://bridge.example.dev/external/amn/publish"
    assert call.kwargs["json"] == {
        "delivery_id": "a" * 64,
        "subreddit": "earthporn",
        "title": "A beautiful landscape",
        "image_url": "https://cdn.example.com/amn/outbox/asset%20photo.jpg",
    }
    assert publisher.session.headers["Authorization"] == "bearer devvit_at_sensitive-token"


def test_devvit_bridge_refuses_publish_without_delivery_identity(tmp_path: Path) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    publisher.delivery_id = None
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_not_called()


def test_devvit_bridge_blocks_ambiguous_submission_and_sets_long_retry(
    tmp_path: Path,
) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    publisher.session.post = MagicMock(
        return_value=_response({"status": "reconciliation_required"}, status_code=202)
    )

    assert publisher.publish(image, "caption") is False
    assert publisher.retry_after == 3600
    assert publisher.last_message_id is None


def test_devvit_bridge_redacts_managed_token_on_http_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    response = _response({}, status_code=401)
    response.raise_for_status.side_effect = requests.HTTPError(
        "unauthorized devvit_at_sensitive-token", response=response
    )
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
    assert "devvit_at_sensitive-token" not in capsys.readouterr().out


def test_devvit_bridge_requires_https_and_managed_token() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        RedditDevvitPublisher(
            "http://bridge.example.dev/external/amn/publish",
            "devvit_at_token",
            "earthporn",
            "https://cdn.example.com/outbox",
        )
    with pytest.raises(ValueError, match="managed endpoint token"):
        RedditDevvitPublisher(
            "https://bridge.example.dev/external/amn/publish",
            "not-a-managed-token",
            "earthporn",
            "https://cdn.example.com/outbox",
        )


def test_devvit_bridge_rejects_files_over_reddit_upload_limit(tmp_path: Path) -> None:
    image = tmp_path / "oversized.jpg"
    with image.open("wb") as handle:
        handle.truncate(20 * 1024 * 1024 + 1)
    publisher = _publisher()
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_not_called()


def test_devvit_bridge_rejects_unsupported_image_format(tmp_path: Path) -> None:
    image = tmp_path / "animation.gif"
    image.write_bytes(b"GIF89a")
    publisher = _publisher()
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_not_called()
