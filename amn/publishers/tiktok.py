"""TikTok Content Posting API photo publisher."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ..publish import Publisher, RateLimiter, _retry_after_from_response

__all__ = ["TikTokPublisher"]


def _truncate_utf16(value: str, max_units: int) -> str:
    result: list[str] = []
    used = 0
    for character in value:
        units = 2 if ord(character) > 0xFFFF else 1
        if used + units > max_units:
            break
        result.append(character)
        used += units
    return "".join(result)


class TikTokPublisher(Publisher):
    """Publish a photo post from a verified, public HTTPS URL.

    TikTok's photo API uses PULL_FROM_URL, so the configured domain/prefix must
    be verified in the TikTok developer app. Direct posting requires video.publish.
    """

    API = "https://open.tiktokapis.com/v2"

    def __init__(
        self,
        access_token: str,
        public_base_url: str,
        privacy_level: str,
        *,
        direct_post_enabled: bool = False,
        timeout: float = 30.0,
        poll_interval: float = 5.0,
        max_polls: int = 24,
    ) -> None:
        parsed = urlparse(public_base_url)
        if not access_token:
            raise ValueError("TikTok user access token is required")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("TikTok media base URL must be public HTTPS")
        if not privacy_level:
            raise ValueError("TikTok privacy_level must be explicitly configured")
        if not direct_post_enabled:
            raise ValueError("TikTok direct posting requires explicit direct_post_enabled=true")
        self.token, self.public_base_url = access_token, public_base_url.rstrip("/")
        self.privacy_level = privacy_level
        self.timeout, self.poll_interval, self.max_polls = timeout, poll_interval, max_polls
        self.session = requests.Session()
        self.rate_limiter = RateLimiter(min_interval=10.0)
        self.retry_after: float | None = None
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def _request(self, endpoint: str, payload: dict) -> dict:
        self.rate_limiter.wait()
        response = self.session.post(
            f"{self.API}/{endpoint}/", headers=self.headers, json=payload, timeout=self.timeout
        )
        response.raise_for_status()
        result = response.json()
        error = result.get("error", {})
        if error.get("code") not in {None, "", "ok"}:
            raise RuntimeError(f"TikTok API {error.get('code')}: {error.get('message', '')}")
        return result

    def publish(self, image_path: Path, caption: str) -> bool:
        self.retry_after = None
        if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            print("TikTok photo posts require a supported image")
            return False
        photo_url = f"{self.public_base_url}/{quote(image_path.name)}"
        try:
            creator = self._request("post/publish/creator_info/query", {})
            options = creator.get("data", {}).get("privacy_level_options", [])
            if self.privacy_level not in options:
                raise RuntimeError(
                    f"configured privacy level {self.privacy_level!r} is not allowed: {options}"
                )

            description = _truncate_utf16(caption, 4000)
            initialized = self._request(
                "post/publish/content/init",
                {
                    "post_info": {
                        "title": _truncate_utf16(description, 90),
                        "description": description,
                        "privacy_level": self.privacy_level,
                    },
                    "source_info": {
                        "source": "PULL_FROM_URL",
                        "photo_cover_index": 0,
                        "photo_images": [photo_url],
                    },
                    "post_mode": "DIRECT_POST",
                    "media_type": "PHOTO",
                },
            )
            publish_id = initialized.get("data", {}).get("publish_id")
            if not publish_id:
                raise RuntimeError("TikTok did not return a publish ID")

            for attempt in range(self.max_polls):
                status = self._request("post/publish/status/fetch", {"publish_id": publish_id})
                state = status.get("data", {}).get("status")
                if state == "PUBLISH_COMPLETE":
                    return True
                if state == "FAILED":
                    raise RuntimeError(status.get("data", {}).get("fail_reason", "post failed"))
                if attempt + 1 < self.max_polls:
                    time.sleep(self.poll_interval)
            raise RuntimeError("TikTok post did not complete before the polling deadline")
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            response = exc.response if isinstance(exc, requests.RequestException) else None
            self.retry_after = _retry_after_from_response(response)
            print(f"TikTok publish failed: {exc}")
            return False
