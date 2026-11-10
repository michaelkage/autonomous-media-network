"""Instagram Graph API image publisher using public, HTTPS-hosted JPEGs."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ..publish import Publisher

__all__ = ["InstagramPublisher"]


class InstagramPublisher(Publisher):
    """Publish a single JPEG through Meta's create-container/publish flow.

    Meta fetches media from a public URL. Configure a public_base_url that serves
    the exact files in outbox; local filesystem paths are never uploaded directly.
    """

    def __init__(self, access_token: str, instagram_user_id: str, public_base_url: str,
                 *, api_version: str = "v25.0", timeout: float = 30.0,
                 poll_interval: float = 60.0, max_polls: int = 5) -> None:
        parsed = urlparse(public_base_url)
        if not access_token or not instagram_user_id:
            raise ValueError("Instagram access token and user ID are required")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Instagram media base URL must be public HTTPS")
        self.token, self.user_id = access_token, instagram_user_id
        self.public_base_url = public_base_url.rstrip("/")
        self.base = f"https://graph.facebook.com/{api_version}"
        self.timeout, self.poll_interval, self.max_polls = timeout, poll_interval, max_polls
        self.session = requests.Session()

    def publish(self, image_path: Path, caption: str) -> bool:
        if image_path.suffix.lower() not in {".jpg", ".jpeg"}:
            print("Instagram requires JPEG images")
            return False
        media_url = f"{self.public_base_url}/{quote(image_path.name)}"
        try:
            created = self.session.post(
                f"{self.base}/{self.user_id}/media",
                data={"image_url": media_url, "caption": caption[:2200],
                      "access_token": self.token},
                timeout=self.timeout,
            )
            created.raise_for_status()
            container_id = created.json().get("id")
            if not container_id:
                raise RuntimeError("Instagram did not return a media container ID")

            ready = False
            for attempt in range(self.max_polls):
                status = self.session.get(
                    f"{self.base}/{container_id}",
                    params={"fields": "status_code", "access_token": self.token},
                    timeout=self.timeout,
                )
                status.raise_for_status()
                code = status.json().get("status_code")
                if code == "FINISHED":
                    ready = True
                    break
                if code in {"ERROR", "EXPIRED"}:
                    raise RuntimeError(f"Instagram container status: {code}")
                if attempt + 1 < self.max_polls:
                    time.sleep(self.poll_interval)
            if not ready:
                raise RuntimeError("Instagram media container did not become ready in time")

            published = self.session.post(
                f"{self.base}/{self.user_id}/media_publish",
                data={"creation_id": container_id, "access_token": self.token},
                timeout=self.timeout,
            )
            published.raise_for_status()
            if not published.json().get("id"):
                raise RuntimeError("Instagram did not confirm media publication")
            return True
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            print(f"Instagram publish failed: {exc}")
            return False
