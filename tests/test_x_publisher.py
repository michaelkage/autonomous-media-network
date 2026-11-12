from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from amn.distribution import validate_config
from amn.publishers.x import XPublisher


def _response(payload: dict, status_code: int = 200, headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_x_uploads_photo_then_creates_post_and_retains_post_id(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image-bytes")
    publisher = XPublisher("user-token")
    publisher.session.post = MagicMock(
        side_effect=[
            _response({"data": {"id": "media-123"}}),
            _response({"data": {"id": "post-456", "text": "caption"}}),
        ]
    )

    assert publisher.publish(image, "caption") is True
    assert publisher.last_message_id == "post-456"
    upload, create = publisher.session.post.call_args_list
    assert upload.args[0].endswith("/2/media/upload")
    assert base64.b64decode(upload.kwargs["json"]["media"]) == b"image-bytes"
    assert upload.kwargs["json"]["media_category"] == "tweet_image"
    assert create.args[0].endswith("/2/tweets")
    assert create.kwargs["json"] == {
        "text": "caption",
        "media": {"media_ids": ["media-123"]},
    }


def test_x_redacts_token_and_preserves_retry_after(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = XPublisher("sensitive-user-token")
    response = _response({"detail": "bad sensitive-user-token"}, 429, {"Retry-After": "9"})
    response.raise_for_status.side_effect = requests.HTTPError("rate limited", response=response)
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
    assert publisher.retry_after == 9
    assert "sensitive-user-token" not in capsys.readouterr().out


def test_x_rejects_image_over_limit_without_network_call(tmp_path: Path) -> None:
    image = tmp_path / "large.jpg"
    image.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    publisher = XPublisher("token")
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_not_called()


def test_x_config_accepts_user_token_environment_name() -> None:
    config = {
        "targets": [
            {
                "id": "x-main",
                "platform": "x",
                "enabled": True,
                "token_env": "X_USER_ACCESS_TOKEN",
            }
        ],
        "routes": {"default": ["x-main"]},
    }
    assert validate_config(config) == config


def test_x_rejects_malformed_media_upload_response(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = XPublisher("user-token")
    publisher.session.post = MagicMock(return_value=_response({"data": None}))

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_called_once()


def test_x_rejects_non_jpeg_before_network_call(tmp_path: Path) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(b"image-bytes")
    publisher = XPublisher("user-token")
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "30"])
def test_x_rejects_invalid_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="finite"):
        XPublisher("user-token", timeout=timeout)  # type: ignore[arg-type]

def test_x_does_not_log_provider_controlled_error_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = XPublisher("user-token")
    publisher.session.post = MagicMock(
        return_value=_response({"detail": "provider diagnostic with internal account data"})
    )

    assert publisher.publish(image, "caption") is False
    output = capsys.readouterr().out
    assert "provider diagnostic" not in output
    assert "internal account data" not in output
    assert "RuntimeError" in output

