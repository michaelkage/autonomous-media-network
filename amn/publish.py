"""Publishing boundary with retry control, root-safe paths and durable idempotency."""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

__all__ = ["Publisher", "MockPublisher", "BlueskyPublisher", "RateLimiter", "publish_manifest"]


class RateLimiter:
    """Thread-safe token spacing plus bounded exponential retry delay."""

    def __init__(self, min_interval: float = 1.0, max_backoff: float = 60.0) -> None:
        self.min_interval, self.max_backoff = min_interval, max_backoff
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.min_interval
        if delay:
            time.sleep(delay)

    def retry_delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(self.max_backoff, max(0.0, retry_after))
        return min(self.max_backoff, 2**attempt + random.random())


class Publisher(ABC):
    @abstractmethod
    def publish(self, image_path: Path, caption: str) -> bool:
        """Publish one image and return whether the remote service accepted it."""


class MockPublisher(Publisher):
    def publish(self, image_path: Path, caption: str) -> bool:
        print(f"[MOCK PUBLISH] {image_path.name} | Caption: {caption}")
        return True


class BlueskyPublisher(Publisher):
    def __init__(self, handle: str, app_password: str) -> None:
        try:
            from atproto import Client
        except ModuleNotFoundError as exc:
            raise RuntimeError('Install Bluesky support: pip install -e ".[bluesky]"') from exc
        self.client = Client()
        self.client.login(handle, app_password)

    def publish(self, image_path: Path, caption: str) -> bool:
        try:
            with image_path.open("rb") as image:
                blob = self.client.upload_blob(image.read())
            self.client.send_post(
                text=caption,
                embed=self.client.create_embed(
                    image=blob, alt="Image published by Autonomous Media Network"
                ),
            )
            return True
        except Exception as exc:
            print(f"Bluesky publish failed: {exc}")
            return False


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"publishing state is corrupt: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"publishing state must be an object: {path}")
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def publish_manifest(
    manifest_path: Path,
    publisher: Publisher,
    *,
    root: Path | None = None,
    platform: str | None = None,
    state_path: Path | None = None,
    retries: int = 3,
) -> int:
    """Publish eligible manifest records once per platform and content digest.

    State is a local JSON file, not a database. A failed item remains retryable.
    """
    manifest_path = Path(manifest_path).resolve()
    root = Path(root).resolve() if root else manifest_path.parent.parent.resolve()
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}")
        return 0
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    passed = data.get("passed", [])
    platform_key = platform or type(publisher).__name__.lower()
    state_file = state_path or root / "build" / "publish-state.json"
    state = _load_state(state_file)
    published = state.setdefault(platform_key, {})
    limiter = RateLimiter(min_interval=0.25)
    successes = 0

    for item in passed:
        relative = Path(item["output"])
        image_path = (root / relative).resolve()
        if not image_path.is_relative_to(root):
            print(f"refusing path outside project root: {relative}")
            continue
        if not image_path.is_file():
            print(f"publish asset missing: {image_path}")
            continue
        digest = item.get("sha256") or hashlib.sha256(image_path.read_bytes()).hexdigest()
        if digest in published:
            continue
        caption = item.get("caption", "No caption provided.")
        for attempt in range(retries + 1):
            limiter.wait()
            if publisher.publish(image_path, caption):
                published[digest] = {"source": item.get("source"), "published_at": time.time()}
                _save_state(state_file, state)
                successes += 1
                break
            if attempt < retries:
                time.sleep(limiter.retry_delay(attempt))
    return successes
