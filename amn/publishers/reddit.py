"""Reddit Data API link-post publisher for publicly hosted AMN media."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ..publish import Publisher, _retry_after_from_response

__all__ = ["RedditPublisher"]

_MAX_TITLE_LENGTH = 300


class RedditPublisher(Publisher):
    """Submit a link post pointing to a publicly hosted AMN image.

    Reddit's supported Data API submit flow creates a link post here. It does
    not upload a native Reddit image post; the linked asset must be public HTTPS.
    """

    def __init__(
        self,
        access_token: str,
        subreddit: str,
        public_base_url: str,
        user_agent: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not access_token:
            raise ValueError("Reddit OAuth access token is required")
        if not isinstance(subreddit, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
            raise ValueError("a valid subreddit name is required")
        parsed = urlparse(public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Reddit media URL must be public HTTPS without query or fragment")
        if not user_agent.strip() or len(user_agent) > 200:
            raise ValueError("a truthful Reddit API User-Agent (max 200 chars) is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.token = access_token
        self.subreddit = subreddit
        self.public_base_url = public_base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "User-Agent": user_agent,
            }
        )
        self.retry_after: float | None = None
        self.last_message_id: str | None = None

    def publish(self, image_path: Path, caption: str) -> bool:
        self.retry_after = None
        self.last_message_id = None
        try:
            image_path = Path(image_path)
            if not image_path.is_file():
                raise FileNotFoundError("Reddit media asset is missing")
            title = " ".join(caption.split())[:_MAX_TITLE_LENGTH].strip()
            if not title:
                title = image_path.stem[:_MAX_TITLE_LENGTH]
            image_url = f"{self.public_base_url}/{quote(image_path.name)}"
            response = self.session.post(
                "https://oauth.reddit.com/api/submit",
                data={
                    "sr": self.subreddit,
                    "kind": "link",
                    "title": title,
                    "url": image_url,
                    "resubmit": "false",
                    "sendreplies": "true",
                },
                timeout=self.timeout,
            )
            self.retry_after = _retry_after_from_response(response)
            remaining = response.headers.get("X-Ratelimit-Remaining")
            reset = response.headers.get("X-Ratelimit-Reset")
            if self.retry_after is None and remaining is not None and reset is not None:
                try:
                    if float(remaining) <= 0:
                        self.retry_after = max(0.0, float(reset))
                except (TypeError, ValueError):
                    pass
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Reddit returned an invalid submission response")
            result = payload.get("json", {})
            errors = result.get("errors", []) if isinstance(result, dict) else []
            if errors:
                safe_errors = str(errors).replace(self.token, "[REDACTED]")[:300]
                raise RuntimeError(f"Reddit rejected the submission: {safe_errors}")
            data = result.get("data", {}) if isinstance(result, dict) else {}
            if not isinstance(data, dict):
                raise RuntimeError("Reddit submission response was malformed")
            post_id = data.get("id") or data.get("name")
            if not isinstance(post_id, str) or not post_id:
                raise RuntimeError("Reddit accepted no identifiable post receipt")
            self.last_message_id = post_id
            return True
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            message = str(exc).replace(self.token, "[REDACTED]")[:300]
            print(f"Reddit publish failed: {message}")
            return False
