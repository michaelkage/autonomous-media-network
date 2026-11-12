"""Facebook Pages Graph API photo publisher."""

from __future__ import annotations

import math
import re
from pathlib import Path

import requests

from ..publish import Publisher, _retry_after_from_response

__all__ = ["FacebookPagePublisher"]

_MAX_PHOTO_BYTES = 10 * 1024 * 1024


class FacebookPagePublisher(Publisher):
    """Publish one photo to a Facebook Page using a Page access token."""

    def __init__(
        self,
        page_access_token: str,
        page_id: str,
        *,
        api_version: str = "v26.0",
        timeout: float = 30.0,
    ) -> None:
        if not isinstance(page_access_token, str) or not page_access_token.strip():
            raise ValueError("Facebook Page access token is required")
        if isinstance(page_id, bool) or not str(page_id).isdigit():
            raise ValueError("Facebook Page ID must contain only decimal digits")
        if not isinstance(api_version, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+", api_version):
            raise ValueError("api_version must use the format vMAJOR.MINOR")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        self.token = page_access_token
        self.page_id = str(page_id)
        self.base = f"https://graph.facebook.com/{api_version}"
        self.timeout = timeout
        self.session = requests.Session()
        self.retry_after: float | None = None
        self.last_message_id: str | None = None

    def publish(self, image_path: Path, caption: str) -> bool:
        self.retry_after = None
        self.last_message_id = None
        try:
            image_path = Path(image_path)
            if image_path.stat().st_size > _MAX_PHOTO_BYTES:
                raise ValueError("Facebook Page photo uploads are limited to 10 MiB")
            with image_path.open("rb") as image:
                response = self.session.post(
                    f"{self.base}/{self.page_id}/photos",
                    data={
                        "access_token": self.token,
                        "caption": caption,
                        "published": "true",
                    },
                    files={"source": (image_path.name, image, "image/jpeg")},
                    timeout=self.timeout,
                )
            self.retry_after = _retry_after_from_response(response)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Facebook returned an invalid response")
            if payload.get("error"):
                error = payload["error"]
                message = (
                    error.get("message", "Facebook rejected the photo")
                    if isinstance(error, dict)
                    else "Facebook rejected the photo"
                )
                raise RuntimeError(str(message).replace(self.token, "[REDACTED]")[:300])
            post_id = payload.get("post_id") or payload.get("id")
            if not isinstance(post_id, (str, int)) or isinstance(post_id, bool):
                raise RuntimeError("Facebook response did not include a post ID")
            self.last_message_id = str(post_id)
            return True
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            message = str(exc).replace(self.token, "[REDACTED]")[:300]
            print(f"Facebook Page publish failed: {message}")
            return False
