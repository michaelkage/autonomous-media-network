"""Reddit image ingestion with atomic image/sidecar commits."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import requests

__all__ = ["RedditFetcher"]
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
_MAX_IMAGE_BYTES = 25 * 1024 * 1024


class RedditFetcher:
    def __init__(self, user_agent: str = "amn-fetcher-v1") -> None:
        self.user_agent = user_agent
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def fetch(self, subreddit_name: str, limit: int = 10, inbox_dir: Path = Path("inbox")) -> int:
        if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit_name):
            raise ValueError("invalid subreddit name")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        inbox_dir.mkdir(parents=True, exist_ok=True)
        try:
            response = self.session.get(
                f"https://www.reddit.com/r/{subreddit_name}/hot.json",
                params={"limit": limit},
                timeout=(5, 20),
            )
            response.raise_for_status()
            posts = response.json().get("data", {}).get("children", [])
        except (requests.RequestException, ValueError) as exc:
            print(f"failed to reach reddit: {exc}")
            return 0

        saved = 0
        for post in posts:
            payload = post.get("data", {})
            image_url = payload.get("url", "")
            post_id = payload.get("id", "")
            if (
                not re.fullmatch(r"[A-Za-z0-9_]{1,32}", str(post_id))
                or not image_url.startswith("https://")
                or not image_url.lower().split("?", 1)[0].endswith(_IMAGE_SUFFIXES)
            ):
                continue
            image_path, sidecar_path = inbox_dir / f"{post_id}.jpg", inbox_dir / f"{post_id}.json"
            if image_path.exists() and sidecar_path.exists():
                continue
            # Recover from a process crash between the two final renames.
            image_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            temp_image = temp_sidecar = None
            try:
                download = self.session.get(image_url, timeout=(5, 30), stream=True)
                download.raise_for_status()
                content_length = download.headers.get("Content-Length")
                if content_length and int(content_length) > _MAX_IMAGE_BYTES:
                    raise ValueError("image exceeds the 25 MiB download limit")
                metrics = {
                    "likes": max(0, int(payload.get("score") or 0)),
                    "comments": max(0, int(payload.get("num_comments") or 0)),
                    "shares": 0,
                    "source": "reddit",
                    "subreddit": subreddit_name,
                    "source_id": post_id,
                    "permalink": f"https://www.reddit.com{payload.get('permalink', '')}",
                }
                with tempfile.NamedTemporaryFile(dir=inbox_dir, delete=False) as f:
                    temp_image = Path(f.name)
                    total_bytes = 0
                    for chunk in download.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > _MAX_IMAGE_BYTES:
                            raise ValueError("image exceeds the 25 MiB download limit")
                        f.write(chunk)
                    if total_bytes == 0:
                        raise ValueError("empty image response")
                    f.flush()
                    os.fsync(f.fileno())
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=inbox_dir, delete=False
                ) as f:
                    temp_sidecar = Path(f.name)
                    json.dump(metrics, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_image, image_path)
                temp_image = None
                os.replace(temp_sidecar, sidecar_path)
                temp_sidecar = None
                saved += 1
            except (requests.RequestException, OSError, ValueError, TypeError) as exc:
                image_path.unlink(missing_ok=True)
                sidecar_path.unlink(missing_ok=True)
                print(f"failed to fetch {image_url}: {exc}")
            finally:
                if temp_image:
                    temp_image.unlink(missing_ok=True)
                if temp_sidecar:
                    temp_sidecar.unlink(missing_ok=True)
        return saved
