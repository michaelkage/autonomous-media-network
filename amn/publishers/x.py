"""X API v2 image publisher using an OAuth 2.0 user access token."""

from __future__ import annotations

import base64
from pathlib import Path

import requests

from ..publish import Publisher, _retry_after_from_response

__all__ = ["XPublisher"]

_MAX_IMAGE_BYTES = 5_000_000


class XPublisher(Publisher):
    """Upload one JPEG and create an X Post with the resulting media ID."""

    def __init__(self, user_access_token: str, *, timeout: float = 30.0) -> None:
        if not user_access_token:
            raise ValueError("X user access token is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.token = user_access_token
        self.base = "https://api.x.com/2"
        self.timeout = timeout
        self.session = requests.Session()
        self.retry_after: float | None = None
        self.last_message_id: str | None = None

    def _request(self, url: str, **kwargs: object) -> dict:
        response = self.session.post(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
            **kwargs,
        )
        self.retry_after = _retry_after_from_response(response)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("X returned an invalid response")
        if payload.get("errors") or payload.get("detail") or payload.get("title") == "Forbidden":
            detail = payload.get("detail") or payload.get("title") or "X rejected the request"
            raise RuntimeError(str(detail).replace(self.token, "[REDACTED]")[:300])
        return payload

    def publish(self, image_path: Path, caption: str) -> bool:
        self.retry_after = None
        self.last_message_id = None
        try:
            image_path = Path(image_path)
            size = image_path.stat().st_size
            if size > _MAX_IMAGE_BYTES:
                raise ValueError("X image uploads are limited to 5 MB")
            if size == 0:
                raise ValueError("X image upload cannot be empty")
            media = base64.b64encode(image_path.read_bytes()).decode("ascii")
            uploaded = self._request(
                f"{self.base}/media/upload",
                json={
                    "media": media,
                    "media_type": "image/jpeg",
                    "media_category": "tweet_image",
                },
            )
            media_data = uploaded.get("data", uploaded)
            if not isinstance(media_data, dict):
                raise RuntimeError("X media upload response was malformed")
            media_id = media_data.get("id") or media_data.get("media_id")
            if not isinstance(media_id, (str, int)) or isinstance(media_id, bool):
                raise RuntimeError("X media upload response did not include a media ID")

            created = self._request(
                f"{self.base}/tweets",
                json={"text": caption, "media": {"media_ids": [str(media_id)]}},
            )
            post_data = created.get("data", {})
            post_id = post_data.get("id") if isinstance(post_data, dict) else None
            if not isinstance(post_id, (str, int)) or isinstance(post_id, bool):
                raise RuntimeError("X create-post response did not include a Post ID")
            self.last_message_id = str(post_id)
            return True
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            message = str(exc).replace(self.token, "[REDACTED]")[:300]
            print(f"X publish failed: {message}")
            return False
