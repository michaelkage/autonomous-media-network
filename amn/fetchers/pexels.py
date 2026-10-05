"""Pexels fetcher.

Fetches high-quality imagery from Pexels.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

__all__ = ["PexelsFetcher"]


class PexelsFetcher:
    """Downloads high-quality imagery from Pexels.

    Args:
        api_key: Pexels API key.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"Authorization": self.api_key})

    def fetch(
        self,
        query: str,
        limit: int = 10,
        inbox_dir: Path = Path("inbox"),
    ) -> int:
        """Fetch photos based on a search query. Returns count of assets saved."""
        inbox_dir.mkdir(parents=True, exist_ok=True)
        saved_count = 0

        url = f"https://api.pexels.com/v1/search?query={query}&per_page={limit}"
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            print(f"failed to reach pexels: {exc}")
            return 0

        photos = data.get("photos", [])
        for photo in photos:
            # Pexels provides multiple sizes; we take 'large'
            img_url = photo.get("src", {}).get("large")
            if not img_url:
                continue

            # Use Pexels ID as filename
            photo_id = photo.get("id")
            filename = f"pexels_{photo_id}.jpg"
            image_path = inbox_dir / filename
            json_path = inbox_dir / f"pexels_{photo_id}.json"

            if image_path.exists():
                continue

            try:
                img_data = self.session.get(img_url, timeout=10).content
                image_path.write_bytes(img_data)

                # Pexels reports no per-photo engagement: the search API exposes only
                # dimensions, photographer and colour. The old hardcoded
                # {views: 1000, likes: 20} sat exactly on the 2% threshold,
                # which is not data, it is a decision dressed up as a metric.
                # Nothing is invented here, so the vetting engine rejects these
                # on the min_likes floor until a curation exemption exists.
                metrics = {
                    "likes": 0,
                    "comments": 0,
                    "shares": 0,
                }

                json_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
                saved_count += 1
            except (requests.RequestException, OSError) as exc:
                print(f"failed to fetch {img_url}: {exc}")

        return saved_count
