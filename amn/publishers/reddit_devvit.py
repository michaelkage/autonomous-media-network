"""Reddit native-image publisher backed by a trusted Devvit external endpoint."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ..publish import Publisher, _retry_after_from_response

__all__ = ["RedditDevvitPublisher"]

_MAX_TITLE_LENGTH = 300


class RedditDevvitPublisher(Publisher):
    """Delegate Reddit-hosted image upload and native image submission to Devvit.

    The endpoint must be an approved Devvit external endpoint using a managed
    token. Delivery IDs are supplied by AMN's durable publishing boundary.
    """

    def __init__(
        self,
        endpoint_url: str,
        access_token: str,
        subreddit: str,
        public_base_url: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        endpoint = urlparse(endpoint_url)
        media = urlparse(public_base_url)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Devvit bridge endpoint must be an HTTPS URL without credentials")
        if (
            media.scheme != "https"
            or not media.hostname
            or media.username
            or media.password
            or media.query
            or media.fragment
        ):
            raise ValueError("Reddit media URL must be public HTTPS without query or fragment")
        if not access_token.startswith("devvit_at_"):
            raise ValueError("a Devvit managed endpoint token is required")
        if not isinstance(subreddit, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
            raise ValueError("a valid subreddit name is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self.endpoint_url = endpoint_url.rstrip("/")
        self.token = access_token
        self.subreddit = subreddit
        self.public_base_url = public_base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.delivery_id: str | None = None
        self.retry_after: float | None = None
        self.last_message_id: str | None = None
        self.last_permalink: str | None = None

    def publish(self, image_path: Path, caption: str) -> bool:
        self.retry_after = None
        self.last_message_id = None
        self.last_permalink = None
        try:
            image_path = Path(image_path)
            if not image_path.is_file():
                raise FileNotFoundError("Reddit media asset is missing")
            if not self.delivery_id or not re.fullmatch(r"[a-f0-9]{64}", self.delivery_id):
                raise ValueError("AMN delivery identity is required for Devvit publishing")

            title = " ".join(caption.split())[:_MAX_TITLE_LENGTH].strip()
            if not title:
                title = image_path.stem[:_MAX_TITLE_LENGTH]
            image_url = f"{self.public_base_url}/{quote(image_path.name)}"
            payload = {
                "delivery_id": self.delivery_id,
                "subreddit": self.subreddit,
                "title": title,
                "image_url": image_url,
            }
            response = self.session.post(
                self.endpoint_url,
                json=payload,
                timeout=self.timeout,
            )
            self.retry_after = _retry_after_from_response(response)
            if response.status_code == 409 or response.status_code == 202:
                self.retry_after = max(self.retry_after or 0, 3600)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("Devvit bridge returned an invalid response")
            if body.get("status") != "published":
                status = body.get("status")
                if status in {"processing", "unknown", "reconciliation_required"}:
                    # A previous request may have reached Reddit. Do not create a
                    # second post while the provider-side result is ambiguous.
                    self.retry_after = max(self.retry_after or 0, 3600)
                    raise RuntimeError("Devvit delivery is unresolved; automatic resubmission blocked")
                raise RuntimeError("Devvit bridge did not confirm publication")

            post_id = body.get("post_id")
            if not isinstance(post_id, str) or not post_id:
                raise RuntimeError("Devvit bridge returned no Reddit post receipt")
            permalink = body.get("permalink")
            if permalink is not None and not isinstance(permalink, str):
                raise RuntimeError("Devvit bridge returned an invalid permalink")
            if permalink and not permalink.startswith("/r/"):
                raise RuntimeError("Devvit bridge returned an unsafe permalink")

            self.last_message_id = post_id.removeprefix("t3_")
            self.last_permalink = permalink
            return True
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            safe_message = str(exc).replace(self.token, "[REDACTED]")[:300]
            print(f"Reddit Devvit publish failed: {safe_message}")
            return False
