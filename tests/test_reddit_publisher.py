from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from amn.publishers.reddit import RedditPublisher


def _response(payload: dict, status_code: int = 200, headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _publisher() -> RedditPublisher:
    return RedditPublisher(
        "reddit-user-token",
        "earthporn",
        "https://cdn.example.com/amn/outbox",
        "linux:amn:v0.2.0 (by /u/amn_test)",
    )


def test_reddit_submits_link_post_and_retains_submission_id(tmp_path: Path) -> None:
    image = tmp_path / "asset photo.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    publisher.session.post = MagicMock(
        return_value=_response(
            {"json": {"errors": [], "data": {"id": "abc123", "name": "t3_abc123"}}}
        )
    )

    assert publisher.publish(image, "A beautiful landscape") is True
    assert publisher.last_message_id == "abc123"
    call = publisher.session.post.call_args
    assert call.args[0] == "https://oauth.reddit.com/api/submit"
    assert call.kwargs["data"] == {
        "sr": "earthporn",
        "api_type": "json",
        "kind": "link",
        "title": "A beautiful landscape",
        "url": "https://cdn.example.com/amn/outbox/asset%20photo.jpg",
        "sendreplies": "true",
    }
    assert call.kwargs["headers"]["Authorization"] == "Bearer reddit-user-token"
    assert call.kwargs["headers"]["User-Agent"] == "linux:amn:v0.2.0 (by /u/amn_test)"


def test_reddit_truncates_title_to_provider_limit(tmp_path: Path) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    publisher.session.post = MagicMock(
        return_value=_response({"json": {"errors": [], "data": {"id": "abc123"}}})
    )

    assert publisher.publish(image, "x" * 350) is True
    assert len(publisher.session.post.call_args.kwargs["data"]["title"]) == 300


def test_reddit_handles_rate_limit_and_redacts_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    response = _response(
        {"json": {"errors": [["RATELIMIT", "sensitive-token", "title"]]}},
        429,
        {"Retry-After": "17"},
    )
    response.raise_for_status.side_effect = requests.HTTPError("rate limited", response=response)
    publisher.token = "sensitive-token"
    publisher.session.headers["Authorization"] = "Bearer sensitive-token"
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
    assert publisher.retry_after == 17
    assert "sensitive-token" not in capsys.readouterr().out


def test_reddit_uses_reset_header_when_rate_limit_is_exhausted(tmp_path: Path) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    response = _response(
        {"json": {"errors": [["RATELIMIT", "slow down", "title"]]}},
        429,
        {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "42"},
    )
    response.raise_for_status.side_effect = requests.HTTPError("rate limited", response=response)
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
    assert publisher.retry_after == 42


def test_reddit_rejects_malformed_success_without_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "asset.jpg"
    image.write_bytes(b"image")
    publisher = _publisher()
    publisher.session.post = MagicMock(return_value=_response({"json": {"errors": [], "data": {}}}))

    assert publisher.publish(image, "caption") is False
    assert publisher.last_message_id is None
    assert "no identifiable post receipt" in capsys.readouterr().out


def test_reddit_requires_https_media_url_and_user_agent() -> None:
    with pytest.raises(ValueError, match="public HTTPS"):
        RedditPublisher("token", "earthporn", "http://cdn.example.com/outbox", "valid-agent")
    with pytest.raises(ValueError, match="User-Agent"):
        RedditPublisher("token", "earthporn", "https://cdn.example.com/outbox", "")
