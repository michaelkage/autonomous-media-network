"""Reddit fetcher.

Fetches trending images from specified subreddits using public JSON endpoints.
Requires no API keys, only a unique User-Agent.
"""

from __future__ import annotations

import json
from pathlib import Path

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
            url = data_payload.get("url")

            if not url or not any(
                url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")
            ):
                continue

            post_id = data_payload.get("id")
            filename = f"{post_id}.jpg"
            image_path = inbox_dir / filename
            json_path = inbox_dir / f"{post_id}.json"

            if image_path.exists():
                continue

            try:
                # Download Image
                img_data = self.session.get(url, timeout=10).content
                image_path.write_bytes(img_data)

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

                json_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
                saved_count += 1
            except (requests.RequestException, OSError) as exc:
                print(f"failed to fetch {url}: {exc}")

        return saved_count
