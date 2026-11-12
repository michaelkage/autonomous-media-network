from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from amn.distribution import validate_config
from amn.publishers.facebook import FacebookPagePublisher


def _response(payload: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_facebook_page_publishes_photo_and_retains_post_id(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = FacebookPagePublisher("page-secret", "123456")
    publisher.session.post = MagicMock(return_value=_response({"id": "123456_789"}))

    assert publisher.publish(image, "caption") is True
    assert publisher.last_message_id == "123456_789"
    call = publisher.session.post.call_args
    assert call.args[0].endswith("/v26.0/123456/photos")
    assert call.kwargs["data"]["caption"] == "caption"
    assert call.kwargs["data"]["published"] == "true"
    assert call.kwargs["files"]["source"][0] == "photo.jpg"


def test_facebook_page_handles_rate_limit_and_redacts_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image")
    publisher = FacebookPagePublisher("sensitive-page-token", "123456")
    response = _response({"error": {"message": "invalid sensitive-page-token"}}, 429)
    response.headers = {"Retry-After": "12"}
    response.raise_for_status.side_effect = requests.HTTPError(
        "request failed with sensitive-page-token", response=response
    )
    publisher.session.post = MagicMock(return_value=response)

    assert publisher.publish(image, "caption") is False
    assert publisher.retry_after == 12
    output = capsys.readouterr().out
    assert "sensitive-page-token" not in output


def test_facebook_page_rejects_photo_over_limit_without_network_call(tmp_path: Path) -> None:
    image = tmp_path / "large.jpg"
    image.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    publisher = FacebookPagePublisher("token", "123456")
    publisher.session.post = MagicMock()

    assert publisher.publish(image, "caption") is False
    publisher.session.post.assert_not_called()


def test_facebook_page_config_requires_numeric_page_id() -> None:
    with pytest.raises(ValueError, match="facebook_page_id"):
        validate_config(
            {
                "targets": [
                    {
                        "id": "fb-main",
                        "platform": "facebook",
                        "enabled": True,
                        "facebook_page_id": "not-a-page",
                    }
                ],
                "routes": {},
            }
        )


def test_facebook_page_config_accepts_disabled_target() -> None:
    config = {
        "targets": [
            {
                "id": "fb-main",
                "platform": "facebook",
                "enabled": False,
                "facebook_page_id": "123456",
                "token_env": "FACEBOOK_PAGE_ACCESS_TOKEN",
            }
        ],
        "routes": {"default": ["fb-main"]},
    }
    assert validate_config(config) == config


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "30"])
def test_facebook_page_rejects_invalid_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="finite"):
        FacebookPagePublisher("page-token", "123456", timeout=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("page_id", [None, True, False, "123/../../me", "page-123", ""])
def test_facebook_page_rejects_invalid_page_id(page_id: object) -> None:
    with pytest.raises(ValueError, match="Page ID"):
        FacebookPagePublisher("page-token", page_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("api_version", ["26.0", "v26", "v26.0/photos", "v26.0?x=1", ""])
def test_facebook_page_rejects_invalid_api_version(api_version: object) -> None:
    with pytest.raises(ValueError, match="api_version"):
        FacebookPagePublisher("page-token", "123456", api_version=api_version)  # type: ignore[arg-type]


def test_facebook_page_accepts_valid_api_version_and_numeric_page_id() -> None:
    publisher = FacebookPagePublisher("page-token", 123456, api_version="v26.0")
    assert publisher.base == "https://graph.facebook.com/v26.0"
    assert publisher.page_id == "123456"
