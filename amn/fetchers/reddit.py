"""Reddit fetcher.

Fetches trending images from specified subreddits using public JSON endpoints.
Requires no API keys, only a unique User-Agent.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

__all__ = ["RedditFetcher"]


class RedditFetcher:
    """Downloads trending imagery from Reddit using public .json endpoints.

    Args:
        user_agent: Unique string identifying the app to avoid 429s.
    """

    def __init__(self, user_agent: str = "amn-fetcher-v1") -> None:
        self.user_agent = user_agent
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

    def fetch(
        self,
        subreddit_name: str,
        limit: int = 10,
        inbox_dir: Path = Path("inbox"),
    ) -> int:
        """Fetch top posts from a subreddit via public JSON. Returns count of assets saved."""
        if not subreddit_name or any(char in subreddit_name for char in "/?#"):
            raise ValueError("subreddit_name must be a single subreddit name")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        inbox_dir.mkdir(parents=True, exist_ok=True)
        saved_count = 0

        url = f"https://www.reddit.com/r/{subreddit_name}/hot.json?limit={limit}"
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            print(f"failed to reach reddit: {exc}")
            return 0

        posts = data.get("data", {}).get("children", [])
        for post in posts:
            data_payload = post.get("data", {})
            image_url = data_payload.get("url")
            if not image_url:
                continue
            image_suffix = Path(urlparse(image_url).path).suffix.lower()
            if image_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue

            post_id = data_payload.get("id")
            filename = f"{post_id}.jpg"
            image_path = inbox_dir / filename
            json_path = inbox_dir / f"{filename}.json"

            if image_path.exists():
                continue

            try:
                # Download and validate the response before writing final files.
                image_response = self.session.get(image_url, timeout=10)
                image_response.raise_for_status()
                img_data = image_response.content
                if not img_data:
                    raise OSError("downloaded image is empty")

                # Record engagement. Reddit's public JSON exposes upvotes and comments but
                # no view count, so `views` is deliberately omitted: inventing a
                # denominator (the old `score * 20`) pinned the engagement rate
                # at ~5% for every possible input and disabled the vetting gate.
                # amn.vetting falls back to the absolute min_likes floor.
                metrics = {
                    "likes": data_payload.get("score", 0),
                    "comments": data_payload.get("num_comments", 0),
                    "shares": 0,
                }

                # Stage both files first; clean up partial downloads so retries work.
                image_temp = None
                metrics_temp = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=inbox_dir, suffix=image_suffix, delete=False
                    ) as handle:
                        image_temp = Path(handle.name)
                        handle.write(img_data)
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=inbox_dir, suffix=".json", delete=False
                    ) as handle:
                        metrics_temp = Path(handle.name)
                        json.dump(metrics, handle, indent=2)
                    image_temp.replace(image_path)
                    metrics_temp.replace(json_path)
                    saved_count += 1
                except OSError:
                    image_path.unlink(missing_ok=True)
                    raise
                finally:
                    if image_temp:
                        image_temp.unlink(missing_ok=True)
                    if metrics_temp:
                        metrics_temp.unlink(missing_ok=True)
            except (requests.RequestException, OSError) as exc:
                print(f"failed to fetch {image_url}: {exc}")

        return saved_count
