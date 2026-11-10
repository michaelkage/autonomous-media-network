"""Publishing boundary with retry control, root-safe paths and durable idempotency."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

__all__ = ["Publisher", "MockPublisher", "BlueskyPublisher", "RateLimiter", "publish_manifest"]


_LIMITERS_LOCK = threading.Lock()
_LIMITERS: dict[str, RateLimiter] = {}
_PUBLISH_LOCKS_LOCK = threading.Lock()
_PUBLISH_LOCKS: dict[str, threading.Lock] = {}


def _publish_lock(key: str) -> threading.Lock:
    with _PUBLISH_LOCKS_LOCK:
        return _PUBLISH_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _cross_process_publish_lock(state_file: Path, platform: str):
    """Hold a per-target SQLite write lock across delivery and state persistence.

    SQLite releases the lock automatically if a worker exits unexpectedly. Keep
    this database beside the state file on a local filesystem; it is not intended
    for network filesystems.
    """
    state_file = state_file.resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{state_file}::{platform}".encode()).hexdigest()[:20]
    lock_path = state_file.parent / f".{state_file.name}.{key}.lock.sqlite3"
    connection = sqlite3.connect(lock_path, timeout=300)
    try:
        connection.execute("PRAGMA busy_timeout = 300000")
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _retry_after_from_response(response: Any) -> float | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


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
            return max(0.0, retry_after)
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
    import os
    import tempfile

    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(name).replace(path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _publish_manifest_unlocked(
    manifest_path: Path,
    publisher: Publisher,
    *,
    root: Path | None = None,
    platform: str | None = None,
    state_path: Path | None = None,
    retries: int = 3,
) -> int:
    """Publish eligible manifest records once per platform and content digest.

    The caller must hold the per-target cross-process lock. State remains a
    backwards-compatible atomic JSON ledger; failed items remain retryable.
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
    if not isinstance(published, dict):
        raise ValueError(f"publishing state for {platform_key!r} must be an object")
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.setdefault(platform_key, RateLimiter(min_interval=0.25))
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
        delivery_key = f"{digest}:{relative.as_posix()}"
        if delivery_key in published:
            continue
        caption = item.get("caption", "No caption provided.")
        from .delivery_events import record_delivery_event

        event_database = root / "build" / "jobs.sqlite3"
        for attempt in range(retries + 1):
            limiter.wait()
            attempt_number = attempt + 1
            record_delivery_event(
                event_database,
                platform=platform_key,
                delivery_key=delivery_key,
                output=relative.as_posix(),
                event_type="attempt_started",
                attempt=attempt_number,
            )
            try:
                accepted = publisher.publish(image_path, caption)
            except Exception:
                record_delivery_event(
                    event_database,
                    platform=platform_key,
                    delivery_key=delivery_key,
                    output=relative.as_posix(),
                    event_type="attempt_failed",
                    attempt=attempt_number,
                )
                raise
            if accepted:
                record_delivery_event(
                    event_database,
                    platform=platform_key,
                    delivery_key=delivery_key,
                    output=relative.as_posix(),
                    event_type="provider_accepted",
                    attempt=attempt_number,
                )
                published[delivery_key] = {
                    "digest": digest,
                    "output": relative.as_posix(),
                    "source": item.get("source"),
                    "published_at": time.time(),
                }
                _save_state(state_file, state)
                record_delivery_event(
                    event_database,
                    platform=platform_key,
                    delivery_key=delivery_key,
                    output=relative.as_posix(),
                    event_type="published",
                    attempt=attempt_number,
                )
                successes += 1
                break

            retry_after = getattr(publisher, "retry_after", None)
            will_retry = attempt < retries
            record_delivery_event(
                event_database,
                platform=platform_key,
                delivery_key=delivery_key,
                output=relative.as_posix(),
                event_type="attempt_failed",
                attempt=attempt_number,
                will_retry=will_retry,
                retry_after_seconds=retry_after,
            )
            if will_retry:
                delay = limiter.retry_delay(attempt, retry_after)
                record_delivery_event(
                    event_database,
                    platform=platform_key,
                    delivery_key=delivery_key,
                    output=relative.as_posix(),
                    event_type="retry_scheduled",
                    attempt=attempt_number,
                    will_retry=True,
                    retry_after_seconds=delay,
                )
                time.sleep(delay)
    return successes


def publish_manifest(
    manifest_path: Path,
    publisher: Publisher,
    *,
    root: Path | None = None,
    platform: str | None = None,
    state_path: Path | None = None,
    retries: int = 3,
) -> int:
    """Serialize deliveries per target across threads and local worker processes.

    A per-target SQLite write lock protects the read/publish/write sequence, so
    concurrent workers cannot both publish an item based on stale JSON state.
    A crash after remote acceptance but before the state write can still cause
    an at-least-once duplicate; provider-side idempotency is required to close it.
    """
    manifest_path = Path(manifest_path).resolve()
    resolved_root = Path(root).resolve() if root else manifest_path.parent.parent.resolve()
    platform_key = platform or type(publisher).__name__.lower()
    state_file = Path(state_path) if state_path else resolved_root / "build" / "publish-state.json"
    lock_key = f"{state_file.resolve()}::{platform_key}"
    with _publish_lock(lock_key):
        with _cross_process_publish_lock(state_file, platform_key):
            return _publish_manifest_unlocked(
                manifest_path,
                publisher,
                root=resolved_root,
                platform=platform_key,
                state_path=state_file,
                retries=retries,
            )
